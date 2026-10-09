"""Stage Python runtime sources, replacing reviewed embedded credential literals only."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.66"
SOURCE = ROOT.parent / "superbrowser_process"
INPUTS = ROOT / f"build/release_inputs_{VERSION}"
TARGET = INPUTS / "superbrowser_source"
MANIFEST = INPUTS / "superbrowser_staging_manifest.json"
SOURCE_DIRECTORIES = ("config", "implement", "main", "util")
SANITIZED_FIELDS = {
    "util/db_helper.py": ["password"],
    "util/dingtalk_doc_api.py": ["APP_KEY", "APP_SECRET", "USER_ID"],
    "util/email_util.py": ["username", "auth_code", "get_email_name"],
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sanitize_source(relative: str, payload: bytes) -> tuple[bytes, list[str]]:
    expected = SANITIZED_FIELDS.get(relative, [])
    if not expected:
        return payload, []
    # AST locations are UTF-8 byte offsets. Preserve all unrelated bytes and
    # replace only the reviewed literal nodes, including non-ASCII source text.
    text = payload.decode("utf-8-sig")
    utf8 = text.encode("utf-8")
    tree = ast.parse(text)
    offsets = [0]
    for line in utf8.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    nodes: dict[str, ast.Constant] = {}
    if relative == "util/db_helper.py":
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_pg_connection"]
        for function in functions:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "psycopg2" and node.func.attr == "connect":
                    for keyword in node.keywords:
                        if keyword.arg == "password" and isinstance(keyword.value, ast.Constant):
                            nodes["password"] = keyword.value
    else:
        statements = tree.body
        if relative == "util/email_util.py":
            guards = [node for node in tree.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__" and any(isinstance(value, ast.Constant) and value.value == "__main__" for value in node.test.comparators)]
            statements = [statement for guard in guards for statement in guard.body]
        for node in statements:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in expected:
                        nodes[target.id] = node.value
    if set(nodes) != set(expected) or any(not isinstance(node.value, str) for node in nodes.values()):
        raise RuntimeError(f"Reviewed credential locations changed in {relative}; inspect before staging")
    replacements = []
    for key, node in nodes.items():
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        value = b'os.environ.get("PGPASSWORD", "")' if relative == "util/db_helper.py" and key == "password" else b'""'
        replacements.append((start, end, value))
    if relative == "util/db_helper.py" and not any(isinstance(node, ast.Import) and any(alias.name == "os" and alias.asname in (None, "os") for alias in node.names) for node in tree.body):
        location = offsets[functions[0].lineno - 1]
        replacements.append((location, location, b"import os\n\n"))
    for start, end, value in sorted(replacements, reverse=True):
        utf8 = utf8[:start] + value + utf8[end:]
    staged = (b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b"") + utf8
    compile(staged, relative, "exec")
    return staged, list(expected)


def source_files() -> dict[str, bytes]:
    if not SOURCE.is_dir() or SOURCE.is_symlink():
        raise RuntimeError("Superbrowser source is missing or symbolic")
    found = {}
    for directory in SOURCE_DIRECTORIES:
        base = SOURCE / directory
        if not base.is_dir():
            raise RuntimeError("Missing runtime source directory: " + directory)
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(SOURCE)
            if any(part.startswith(".") or part in {"tests", "__pycache__"} for part in relative.parts):
                continue
            if any(item.is_symlink() for item in (path, *path.parents)):
                raise RuntimeError("Refuse symbolic source path: " + relative.as_posix())
            found[relative.as_posix()] = path.read_bytes()
    if not set(SANITIZED_FIELDS).issubset(found):
        raise RuntimeError("Reviewed credential-bearing modules are missing")
    return found


def staged_hashes() -> dict[str, str]:
    found = {}
    for path in sorted(TARGET.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Refuse symbolic staged source path")
        if path.is_file():
            if path.suffix != ".py":
                raise RuntimeError("Non-Python file in staged runtime")
            found[path.relative_to(TARGET).as_posix()] = digest(path.read_bytes())
    return found


def main() -> None:
    source = source_files()
    staged = {name: sanitize_source(name, content)[0] for name, content in source.items()}
    source_hashes = {name: digest(content) for name, content in source.items()}
    expected = {name: digest(content) for name, content in staged.items()}
    if TARGET.exists():
        if staged_hashes() != expected or not MANIFEST.is_file():
            raise RuntimeError("Existing staging differs or lacks provenance; preserve and inspect it")
        report = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if report.get("version") != VERSION or report.get("files") != expected or report.get("source_files") != source_hashes or report.get("sanitized_fields") != SANITIZED_FIELDS:
            raise RuntimeError("Existing staging manifest differs; preserve and inspect it")
        action = "existing_staging_verified"
    else:
        if MANIFEST.exists():
            raise RuntimeError("Manifest exists without staging; preserve and inspect it")
        TARGET.mkdir(parents=True)
        for name, payload in staged.items():
            destination = TARGET / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(payload)
        if staged_hashes() != expected or {name: digest(content) for name, content in source_files().items()} != source_hashes:
            raise RuntimeError("Source or staging changed while copying")
        report = {"version": VERSION, "status": "sanitized_and_verified", "created_at": datetime.now(timezone.utc).isoformat(),
                  "source": str(SOURCE.resolve()), "destination": str(TARGET.resolve()), "python_sources_only": True,
                  "file_count": len(expected), "source_files": source_hashes, "files": expected, "sanitized_fields": SANITIZED_FIELDS,
                  "configuration_inputs": {"util/db_helper.py": ["PGPASSWORD"]}}
        with MANIFEST.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        action = "sanitized_and_verified"
    print(json.dumps({"action": action, "version": VERSION, "file_count": len(expected), "sanitized_fields": SANITIZED_FIELDS,
                      "staging": str(TARGET), "manifest": str(MANIFEST)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
