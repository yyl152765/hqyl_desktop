"""Stage Python runtime sources, replacing reviewed embedded credential literals only."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_public_runtime_configs import PRIVATE_SITE_FIELDS


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.6"
SOURCE = Path(os.environ.get("HQYL_SUPERBROWSER_INPUT", str(ROOT.parent / "superbrowser_process")))
INPUTS = ROOT / f"build/release_inputs_{VERSION}"
TARGET = INPUTS / "superbrowser_source"
MANIFEST = INPUTS / "superbrowser_staging_manifest.json"
SOURCE_DIRECTORIES = ("config", "implement", "main", "util")
GUARD = "main/shopee/ads_recharge_guard.py"

SANITIZED_FIELDS = {
    GUARD: ["BUILTIN_DB_PASSWORD", "GUARD_DB_CONFIG", "GUARD_DB_FALLBACK_HOST", "OVER_LIMIT_RECIPIENTS"],
    "util/db_helper.py": ["password"],
    "main/shopee/ads_recharge_success_log.py": ["SUCCESS_LOG_DOC_ID", "SUCCESS_LOG_DOC_URL"],
    "util/dingtalk_doc_api.py": ["APP_KEY", "APP_SECRET", "USER_ID"],
    "util/email_util.py": ["username", "auth_code", "get_email_name"],
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stage_database(relative, payload):
    raw = payload.decode("utf-8-sig").encode("utf-8")
    tree = ast.parse(raw)
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    changes = []

    def replace(node, value):
        changes.append((offsets[node.lineno - 1] + node.col_offset,
                        offsets[node.end_lineno - 1] + node.end_col_offset, value.encode("utf-8")))

    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    variables = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)}

    def function(name, body):
        original = funcs[name]
        if original.args.args or original.decorator_list:
            raise ValueError("signature changed")
        replace(original, f"def {name}():\n    " + "\n    ".join(body))

    try:
        if relative == GUARD:
            if not isinstance(variables["BUILTIN_DB_PASSWORD"], ast.Constant) or not isinstance(variables["GUARD_DB_CONFIG"], ast.Dict):
                raise ValueError("configuration shape changed")
            for key, expr in (("BUILTIN_DB_PASSWORD", '""'), ("GUARD_DB_CONFIG", "{}"),
                              ("GUARD_DB_FALLBACK_HOST", '""'), ("OVER_LIMIT_RECIPIENTS", "{}")):
                replace(variables[key], expr)
            function("get_guard_connection", ["from backend.ads_database import connect_runtime_database",
                                              "return connect_runtime_database(guard=True)"])
            function("_ensure_guard_tables_once", ["from backend.ads_database import ensure_daily_quotas",
                                                   "ensure_daily_quotas(SITE_CURRENCIES, get_site_daily_limit)"])
            permission = funcs["check_permission"]
            if [a.arg for a in permission.args.args] != ["site_code", "recharge_mode"]:
                raise ValueError("permission signature changed")
            replace(permission, "def check_permission(site_code, recharge_mode):\n"
                    "    from backend.ads_database import authorized_machine\n"
                    "    return PermissionResult(**authorized_machine(current_machine_context(), site_code, recharge_mode))")
            # Adapt the original reservation and accounting queries while retaining their
            # locks, parameters, amount arithmetic, and transaction boundaries.
            for name in ("get_today_quota", "reserve_quota", "confirm_quota", "release_quota",
                         "create_run_log", "finish_run_log", "write_store_log"):
                queries = [node.args[0] for node in ast.walk(funcs[name]) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"]
                if not queries or any(not isinstance(q, ast.Constant) or "shopee_ads_recharge_" not in q.value for q in queries):
                    raise ValueError(f"Unexpected accounting query in {name}")
                for node in queries:
                    query = node.value.replace("shopee_ads_recharge_", "tbl_fin_ads_recharge_")
                    query = query.replace("updated_at = NOW()", "updated_on = NOW(), version = version + 1")
                    if query.lstrip().startswith(("UPDATE", "SELECT")):
                        if query.count("WHERE ") != 1:
                            raise ValueError(f"Unexpected accounting filter in {name}")
                        query = query.replace("WHERE ", "WHERE tenant_id = 1 AND NOT archived AND ")
                    replace(node, repr(query))
            defaults = [n.args[1] for n in ast.walk(funcs["send_over_limit_notification"])
                        if isinstance(n, ast.Call) and ast.unparse(n.func) == "OVER_LIMIT_RECIPIENTS.get" and len(n.args) == 2]
            if len(defaults) != 1:
                raise ValueError("notification shape changed")
            replace(defaults[0], '["站点负责人"]')
        else:
            function("get_pg_connection", ["from backend.ads_database import connect_runtime_database",
                                           "return connect_runtime_database(guard=False)"])
            upsert = funcs["upsert_batch_to_pg"]
            first = next(n for n in upsert.body if not isinstance(n, ast.Expr))
            pos = offsets[first.lineno - 1]
            changes.append((pos, pos, (
                '    if table_name == "shop_browsers":\n'
                '        from backend.ads_database import sync_shop_browsers\n'
                '        return sync_shop_browsers(conn, data_list)\n'
            ).encode()))
            fetch = funcs["fetch_column_from_db"]
            first = next(n for n in fetch.body if not isinstance(n, ast.Expr))
            pos = offsets[first.lineno - 1]
            changes.append((pos, pos, (
                '    if table_name == "shop_browsers":\n'
                '        table_name = "tbl_fin_ads_shop_browser"\n'
                '        scope = "tenant_id = 1 AND NOT archived"\n'
                '        where_clause = f"{scope} AND ({where_clause})" if where_clause else scope\n'
            ).encode()))
            for method, column, clause, varname in (
                    ("query_auth_code", "browser_oauth", "browser_name LIKE %s AND length(browser_oauth) <> 32", "name"),
                    ("query_shop_name", "browser_name", "browser_oauth LIKE %s", "auth")):
                lookup = next(n for n in ast.walk(funcs[method]) if isinstance(n, ast.Call)
                              and ast.unparse(n.func) == "fetch_column_from_db")
                replace(lookup, f'fetch_column_from_db(conn, "shop_browsers", "{column}", "{clause}", (f"%{{{varname}}}%",))')
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Reviewed credential locations changed in {relative}") from exc
    for start, end, value in sorted(changes, reverse=True):
        raw = raw[:start] + value + raw[end:]
    result = (b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b"") + raw
    compile(result, relative, "exec")
    return result, list(SANITIZED_FIELDS[relative])


def sanitize_source(relative: str, payload: bytes) -> tuple[bytes, list[str]]:
    if relative in (GUARD, "util/db_helper.py"):
        return _stage_database(relative, payload)
    if re.fullmatch(r"(main/shopee/(id|my|ph|th|vn)_shopee_ads_recharge_operator_service|implement/shopee/(th|my)_shopee_ads_recharge)\.py", relative):
        raw = payload.decode("utf-8-sig").encode("utf-8")
        tree = ast.parse(raw)
        private = {v.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
                   for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant)
                   and k.value in PRIVATE_SITE_FIELDS and isinstance(v, ast.Constant)
                   and isinstance(v.value, str) and v.value}
        private.update(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                       and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                       and any(isinstance(t, ast.Name) and t.id.endswith(("_DOC_ID", "_DOC_URL")) for t in n.targets)
                       and n.value.value)
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and (n.value in private or re.fullmatch(r"1[3-9][0-9]{9}", n.value))]
        offsets = [0]
        for line in raw.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        for n in sorted(nodes, key=lambda node: (node.lineno, node.col_offset), reverse=True):
            start = offsets[n.lineno - 1] + n.col_offset
            end = offsets[n.end_lineno - 1] + n.end_col_offset
            raw = raw[:start] + b'""' + raw[end:]
        if relative.startswith("main/"):
            updated = ast.parse(raw)
            run = next(n for n in updated.body if isinstance(n, ast.FunctionDef) and n.name == "run_ads_recharge")
            setting = next(n for n in run.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                           and ast.unparse(n.value.func) == "load_runtime_configs")
            lines = raw.splitlines(keepends=True)
            lines.insert(setting.end_lineno, b'    pcfg = _deep_merge_dict(pcfg, getattr(settings, "private_site_settings", {}))\n')
            raw = b"".join(lines)
        else:
            updated = ast.parse(raw)
            method = next(n for n in updated.body if isinstance(n, ast.FunctionDef) and n.name == "_wait_world_card_sms_code")
            assignments = {n.targets[0].id: n for n in method.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
            connection = next(n for n in ast.walk(method) if isinstance(n, ast.Call) and ast.unparse(n.func) == "psycopg2.connect")
            if len(connection.keywords) != 1 or ast.unparse(connection.keywords[0].value) != "db_config":
                raise ValueError("Unexpected WORLD card database connection")
            query = assignments["sql"].value
            if not isinstance(query, ast.Constant) or "public.sms_receiver_message" not in query.value:
                raise ValueError("Unexpected WORLD card receiver source")
            handler = next(n for n in updated.body if isinstance(n, ast.FunctionDef) and n.name == "_handle_worldfirst_id_check")
            handoff = next(n for n in ast.walk(handler) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                           and ast.unparse(n.value.func) == "_wait_world_card_sms_code")
            lines = raw.splitlines(keepends=True)
            edits = [(assignments["db_config"].lineno-1, assignments["db_config"].end_lineno,
                      b"    from backend.ads_database import connect_runtime_database\n")]
            original = lines[connection.lineno - 1]
            if b"psycopg2.connect(**db_config)" not in original:
                raise ValueError("Unexpected WORLD card database call")
            edits.append((connection.lineno - 1, connection.lineno, original.replace(b"psycopg2.connect(**db_config)", b"connect_runtime_database()")))
            query_lines = b"".join(lines[query.lineno-1:query.end_lineno]).replace(b"public.sms_receiver_message", b"shopee_ads.world_card_payment_messages")
            edits.append((query.lineno-1, query.end_lineno, query_lines))
            edits.append((handoff.lineno-1, handoff.lineno-1,
                          ('        if cfg.get("verification_mode") == "bank_page":\n'
                           '            return True, "请在银行页面输入短信验证码并提交，随后会核对支付结果。"\n').encode()))
            for start, end, replacement in sorted(edits, reverse=True):
                lines[start:end] = [replacement]
            raw = b"".join(lines)
            method = next(n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name == "_wait_world_card_sms_code")
            assignment = next(n for n in method.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "sim_number")
            lines = raw.splitlines(keepends=True)
            lines.insert(assignment.end_lineno, '    if not sim_number:\n        raise ValueError("请先配置此站点的 WORLD 卡验证手机号。")\n'.encode())
            raw = b"".join(lines)
        compile(raw, relative, "exec")
        return raw, ["private_site_settings"] if nodes else []
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
        replacements.append((start, end, b'""'))
    for start, end, value in sorted(replacements, reverse=True):
        utf8 = utf8[:start] + value + utf8[end:]
    staged = (b"\xef\xbb\xbf" if payload.startswith(b"\xef\xbb\xbf") else b"") + utf8
    compile(staged, relative, "exec")
    if relative == "main/shopee/ads_recharge_success_log.py":
        tree = ast.parse(staged)
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "completion_recipients")
        fallback = next(n for n in method.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "fallback_names")
        lines = staged.splitlines(keepends=True)
        lines[fallback.lineno - 1:method.end_lineno] = [b"    return list(fallback_names or [])\n"]
        staged = b"".join(lines)
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
                  "configuration_inputs": {"util/db_helper.py": ["HQYL_GUARD_DB_*", "PGPASSWORD"], "main/shopee/ads_recharge_guard.py": ["HQYL_GUARD_DB_*", "PGPASSWORD"]}}
        with MANIFEST.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        action = "sanitized_and_verified"
    print(json.dumps({"action": action, "version": VERSION, "file_count": len(expected), "sanitized_fields": SANITIZED_FIELDS,
                      "staging": str(TARGET), "manifest": str(MANIFEST)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
