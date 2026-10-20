"""Build a narrowly scoped Excel repair from an existing 0.2.76 frozen app.

Never installs the repair or reads runtime configuration. All untouched frozen
module blobs and archive entries are copied verbatim, rather than rebuilt from
this older source checkout. Requires PyInstaller/pefile in the build interpreter
and an existing Python 3.12 interpreter for compiling the replacement module.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import marshal
from pathlib import Path
import struct
import subprocess
import zlib

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
MODULE = "backend.services.lazada_campaign_workbook"
BASE_SHA256 = "3d680e7e08a9d69fe40455bffb1537993a57560e4094e3ee8d4cc66bbfe98367"
COOKIE = "!8sIIII64s"
ENTRY = "!IIIIBc"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compile_312(python: Path, source: str, filename: str) -> tuple[bytes, bytes]:
    command = (
        "import base64,importlib.util,json,marshal,sys;"
        "assert sys.version_info[:2] == (3,12), sys.version;"
        "p=json.load(sys.stdin);"
        "c=compile(p['source'],p['filename'],'exec',dont_inherit=True,optimize=0);"
        "print(json.dumps({'magic':importlib.util.MAGIC_NUMBER.hex(),"
        "'code':base64.b64encode(marshal.dumps(c)).decode()}))"
    )
    completed = subprocess.run(
        [str(python), "-I", "-B", "-c", command],
        input=json.dumps({"source": source, "filename": filename}),
        text=True, encoding="utf-8", capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)
    return bytes.fromhex(result["magic"]), base64.b64decode(result["code"])


def replace_pyz(original: bytes, module: str, code: bytes, magic: bytes) -> bytes:
    if original[:4] != b"PYZ\0" or original[4:8] != magic:
        raise ValueError("Replacement compiler does not match the frozen Python magic")
    old_toc_offset = struct.unpack_from("!i", original, 8)[0]
    entries = marshal.loads(original[old_toc_offset:])
    if len([name for name, _ in entries if name == module]) != 1:
        raise ValueError("Expected exactly one replacement module")
    payload = bytearray(original[:17])
    new_entries = []
    for name, (kind, offset, length) in entries:
        blob = zlib.compress(code, 6) if name == module else original[offset:offset + length]
        new_entries.append((name, (kind, len(payload), len(blob))))
        payload.extend(blob)
    struct.pack_into("!i", payload, 8, len(payload))
    payload.extend(marshal.dumps(new_entries))
    return bytes(payload)


def replace_archive(base: bytes, changes: dict[str, bytes]) -> bytes:
    cookie_length = struct.calcsize(COOKIE)
    old_cookie = struct.unpack(COOKIE, base[-cookie_length:])
    magic, archive_length, toc_offset, toc_length, pyvers, library = old_cookie
    if magic != b"MEI\x0c\x0b\x0a\x0b\x0e" or pyvers != 312:
        raise ValueError("Expected the original trailing Python 3.12 CArchive")
    start = len(base) - archive_length
    old_toc = base[start + toc_offset:start + toc_offset + toc_length]
    new_payload, new_toc = bytearray(), bytearray()
    cursor, seen = 0, set()
    while cursor < len(old_toc):
        size, offset, compressed, uncompressed, flag, kind = struct.unpack_from(ENTRY, old_toc, cursor)
        name_bytes = old_toc[cursor + struct.calcsize(ENTRY):cursor + size]
        name = name_bytes.rstrip(b"\0").decode("utf-8")
        blob = base[start + offset:start + offset + compressed]
        if name in changes:
            replacement = changes[name]
            blob = zlib.compress(replacement, 9) if flag else replacement
            uncompressed = len(replacement)
            seen.add(name)
        new_toc.extend(struct.pack(ENTRY, size, len(new_payload), len(blob), uncompressed, flag, kind))
        new_toc.extend(name_bytes)
        new_payload.extend(blob)
        cursor += size
    if seen != set(changes):
        raise ValueError("Replacement archive entries are missing")
    new_cookie = struct.pack(COOKIE, magic, len(new_payload) + len(new_toc) + cookie_length,
                             len(new_payload), len(new_toc), pyvers, library)
    candidate = bytearray(base[:start] + new_payload + new_toc + new_cookie)
    image = pefile.PE(data=bytes(candidate), fast_load=True)
    checksum_offset = image.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum")
    struct.pack_into("<I", candidate, checksum_offset, image.generate_checksum())
    image.close()
    return bytes(candidate)


def compressed_module(reader, name: str) -> bytes:
    _kind, offset, size = reader.toc[name]
    with open(reader._filename, "rb") as stream:
        stream.seek(reader._start_offset + offset)
        return stream.read(size)


def verify(base_path: Path, candidate_path: Path, expected: bytes) -> dict:
    old, new = CArchiveReader(str(base_path)), CArchiveReader(str(candidate_path))
    if old.options != new.options or old.toc.keys() != new.toc.keys():
        raise ValueError("CArchive membership/options changed")
    old_z, new_z = old.open_embedded_archive("PYZ.pyz"), new.open_embedded_archive("PYZ.pyz")
    if old_z.toc.keys() != new_z.toc.keys():
        raise ValueError("Frozen module membership changed")
    for name in old.toc:
        if name != "PYZ.pyz" and old.extract(name) != new.extract(name):
            raise ValueError(f"Unrelated CArchive member changed: {name}")
    changed = [name for name in old_z.toc
               if compressed_module(old_z, name) != compressed_module(new_z, name)]
    if changed != [MODULE] or new_z.extract(MODULE, raw=True) != expected:
        raise ValueError(f"Unexpected changed frozen modules: {changed}")
    before, after = base_path.read_bytes(), candidate_path.read_bytes()
    prefix_before, prefix_after = bytearray(before[:old._start_offset]), bytearray(after[:new._start_offset])
    image = pefile.PE(data=before, fast_load=True)
    checksum_offset = image.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum")
    image.close()
    prefix_before[checksum_offset:checksum_offset + 4] = b"\0" * 4
    prefix_after[checksum_offset:checksum_offset + 4] = b"\0" * 4
    if prefix_before != prefix_after:
        raise ValueError("Bootloader changed beyond the PE checksum field")
    image = pefile.PE(data=after, fast_load=True)
    if image.OPTIONAL_HEADER.CheckSum != image.generate_checksum():
        raise ValueError("Candidate PE checksum is invalid")
    image.close()
    return {"changed_modules": changed, "unchanged_pyz_modules": len(old_z.toc) - 1,
            "unchanged_carchive_members": len(old.toc) - 1, "carchive_options_preserved": True,
            "bootloader_preserved_except_pe_checksum": True, "pe_checksum_valid": True,
            "old_module_marshal_sha256": digest(old_z.extract(MODULE, raw=True)),
            "new_module_marshal_sha256": digest(expected)}


def repair_powershell(original_sha: str, candidate_sha: str, default_install: Path) -> str:
    return r'''param([string]$InstallDirectory = '__INSTALL__')
$ErrorActionPreference = 'Stop'
$OriginalSha256 = '__ORIGINAL__'
$CandidateSha256 = '__CANDIDATE__'
$Candidate = Join-Path $PSScriptRoot 'HQYLAutomation.exe'
$InstallRoot = (Resolve-Path -LiteralPath $InstallDirectory).Path
$Target = Join-Path $InstallRoot 'HQYLAutomation.exe'
function Get-Sha([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Assert-Closed {
    foreach ($Process in @(Get-Process -Name HQYLAutomation -ErrorAction SilentlyContinue)) {
        try { $ProcessPath = $Process.Path } catch { throw '无法确认工作台是否关闭，请先关闭工作台后再运行修复。' }
        if (-not $ProcessPath -or $ProcessPath -ieq $Target) {
            throw '请先手动关闭工作台，然后重新运行修复。本脚本不会结束任何进程。'
        }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot '_internal\python312.dll'))) {
    throw '请选择现有工作台的安装目录（其中应有 _internal\python312.dll）。'
}
if ((Get-Sha $Candidate) -ne $CandidateSha256) { throw '修复文件校验失败，请重新获取完整修复包。' }
$TargetSha = Get-Sha $Target
if ($TargetSha -eq $CandidateSha256) { Write-Host '当前工作台已应用此修复，无需重复操作。'; exit 0 }
if ($TargetSha -ne $OriginalSha256) { throw '当前工作台文件与目标 0.2.76 原版不一致，已停止，未替换任何文件。' }
Assert-Closed
$BackupDirectory = Join-Path $InstallRoot 'repair_backups\Excel折扣筛选_0.2.76_3d680e7e'
New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
$Backup = Join-Path $BackupDirectory 'HQYLAutomation.exe'
if (Test-Path -LiteralPath $Backup) {
    if ((Get-Sha $Backup) -ne $OriginalSha256) { throw '已有备份校验失败，已停止。' }
} else {
    Copy-Item -LiteralPath $Target -Destination $Backup
    if ((Get-Sha $Backup) -ne $OriginalSha256) { throw '原文件备份校验失败，已停止。' }
}
$Staged = Join-Path $InstallRoot ('.HQYLAutomation.ExcelRepair.' + $PID + '.tmp')
try {
    Copy-Item -LiteralPath $Candidate -Destination $Staged
    if ((Get-Sha $Staged) -ne $CandidateSha256) { throw '暂存修复文件校验失败。' }
    Assert-Closed
    if ((Get-Sha $Target) -ne $OriginalSha256) { throw '工作台文件在修复期间发生变化，已停止。' }
    [System.IO.File]::Replace($Staged, $Target, $null)
    if ((Get-Sha $Target) -ne $CandidateSha256) { throw '修复后校验失败，请从 repair_backups 恢复备份。' }
    Write-Host 'Excel 折扣筛选局部修复完成。请重新打开工作台，再导入原始 Excel。'
    Write-Host ('原版备份：' + $Backup)
} finally {
    if (Test-Path -LiteralPath $Staged) { Remove-Item -LiteralPath $Staged }
}
'''.replace("__ORIGINAL__", original_sha).replace("__CANDIDATE__", candidate_sha).replace(
        "__INSTALL__", str(default_install).replace("'", "''"))


PROBE = '''import json, pathlib, socket, sys, traceback
def denied(*args, **kwargs):
    raise RuntimeError("Network access is forbidden in this local workbook probe")
socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
out = pathlib.Path(sys.argv[2])
try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    if set(payload) - {"source_files", "output_dir", "mode", "threshold"}:
        raise ValueError("Unexpected probe input fields")
    from backend.services.lazada_campaign_workbook import validate_campaign_payload, run_campaign_batch
    result = run_campaign_batch(validate_campaign_payload(payload))
    report = {"ok": True, "result": result}
except Exception as exc:
    report = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path(r"E:\软件\HQYLAutomation\HQYLAutomation.exe"))
    parser.add_argument("--python312", type=Path, default=Path.home() / "AppData/Roaming/uv/python/cpython-3.12.14-windows-x86_64-none/python.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "release/Excel折扣筛选_0.2.76_修复包")
    parser.add_argument("--probe-exe", type=Path)
    args = parser.parse_args()
    original = args.base.read_bytes()
    if digest(original) != BASE_SHA256:
        raise ValueError("Base executable does not match the reviewed original 0.2.76 SHA256")
    source_path = ROOT / "backend/services/lazada_campaign_workbook.py"
    source = source_path.read_text(encoding="utf-8-sig")
    magic, code = compile_312(args.python312, source, "backend/services/lazada_campaign_workbook.py")
    archive = CArchiveReader(str(args.base))
    patched_pyz = replace_pyz(archive.extract("PYZ.pyz"), MODULE, code, magic)
    candidate = replace_archive(original, {"PYZ.pyz": patched_pyz})
    args.output.mkdir(parents=True, exist_ok=True)
    candidate_path = args.output / "HQYLAutomation.exe"
    candidate_path.write_bytes(candidate)
    verified = verify(args.base, candidate_path, code)
    sha = digest(candidate)
    manifest = {"kind": "Excel折扣筛选局部修复", "application_version": "0.2.76",
                "base_executable_sha256": BASE_SHA256, "repair_executable_sha256": sha,
                "source_file": "backend/services/lazada_campaign_workbook.py",
                "source_sha256": digest(source_path.read_bytes()), "python_magic": magic.hex(),
                "runtime_files_read_or_modified": False, "installed": False, **verified}
    (args.output / "修复校验清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "SHA256.txt").write_text(f"{sha}  HQYLAutomation.exe\n", encoding="ascii")
    (args.output / "应用修复.ps1").write_text(repair_powershell(BASE_SHA256, sha, args.base.parent), encoding="utf-8-sig")
    (args.output / "使用说明.txt").write_text(
        "Excel 折扣筛选 0.2.76 局部修复包\n\n"
        "适用：已安装的目标 0.2.76 版本。保留其它模块及当前版本，仅修复 Excel 折扣筛选。\n"
        "支持从推荐价 0 < Price <= 76.91 提取 76.91；识别模板说明行；支持严格匹配同排推荐列的 TEXTAFTER/TRIM 取价公式。\n"
        "此目录未包含运行库，请勿直接双击本目录 HQYLAutomation.exe。修复尚未安装。\n\n"
        "1. 保存工作并手动关闭现有工作台。\n"
        "2. 在本目录打开 PowerShell，执行：\n"
        "   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\应用修复.ps1\n"
        "   安装目录不同时，在命令后加 -InstallDirectory '实际安装目录'。\n"
        "3. 脚本校验原版和修复文件，备份原 exe，然后原子替换；校验不符则停止，不会结束任何进程。\n"
        "4. 重新打开原安装目录工作台，导入原始 Excel，最低折扣率填写 95。\n\n"
        "版本仍显示 0.2.76；是否已修复以修复校验清单及 exe SHA256 为准。重复执行会提示已修复。\n"
        "回退：关闭工作台，将安装目录 repair_backups\\Excel折扣筛选_0.2.76_3d680e7e\\HQYLAutomation.exe 复制回安装目录。\n"
        "安装目录的 _internal、账号设置、用户数据及原 Excel 均不替换。\n", encoding="utf-8-sig")
    if args.probe_exe:
        probe_magic, probe_code = compile_312(args.python312, PROBE, "desktop_entry.py")
        if probe_magic != magic:
            raise ValueError("Probe Python magic mismatch")
        args.probe_exe.parent.mkdir(parents=True, exist_ok=True)
        args.probe_exe.write_bytes(replace_archive(candidate, {"desktop_entry": probe_code}))
        probe_reader = CArchiveReader(str(args.probe_exe))
        if probe_reader.extract("PYZ.pyz") != patched_pyz:
            raise ValueError("Probe does not contain the exact shipping candidate PYZ")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
