from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


SITE_CONFIG_NAMES = (
    "Shopee印尼广告充值.yaml",
    "Shopee泰国广告充值.yaml",
    "Shopee菲律宾广告充值.yaml",
    "Shopee越南广告充值.yaml",
    "Shopee马来广告充值.yaml",
)
BIGSELLER_CONFIG_NAME = "BigSeller库存同步.yaml"


def build_public_process_config(payload: dict[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key in ("task", "thread_num", "database", "shopee", "operator"):
        value = payload.get(key)
        if value not in (None, {}, []):
            public[key] = copy.deepcopy(value)

    # Keep verification flow settings, but never distribute a developer's
    # verification contact or requester identity in a shared installation.
    shopee = public.get("shopee", {})
    security_verify = shopee.get("security_verify", {}) if isinstance(shopee, dict) else {}
    if isinstance(security_verify, dict):
        for key in ("phone", "user_id", "user_name", "requester", "token", "secret"):
            security_verify.pop(key, None)

    doc_config = payload.get("dingtalk", {}).get("doc_config", {})
    if isinstance(doc_config, dict):
        safe_doc_config = {
            key: copy.deepcopy(value)
            for key, value in doc_config.items()
            if str(key).casefold() not in {"app_key", "app_secret", "access_token", "secret"}
        }
        if safe_doc_config:
            public["dingtalk"] = {"doc_config": safe_doc_config}
    return public


def build_public_bigseller_config(payload: dict[str, Any]) -> dict[str, Any]:
    process_config = payload.get("process_config", {})
    if not isinstance(process_config, dict):
        raise ValueError("BigSeller process_config must be a mapping")

    public_process_config: dict[str, Any] = {}
    for key in ("process_name", "user_agent", "request", "browser", "sync"):
        value = process_config.get(key)
        if value not in (None, {}, []):
            public_process_config[key] = copy.deepcopy(value)

    captcha_service = process_config.get("captcha_service", {})
    if isinstance(captcha_service, dict):
        safe_captcha = {
            key: copy.deepcopy(value)
            for key, value in captcha_service.items()
            if str(key).casefold() not in {"username", "password", "token", "secret"}
        }
        if safe_captcha:
            public_process_config["captcha_service"] = safe_captcha
    return {"process_config": public_process_config}


def generate_public_runtime_configs(source_dir: Path, output_dir: Path) -> list[Path]:
    target_dir = output_dir / "main" / "shopee" / "config"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    for name in SITE_CONFIG_NAMES:
        source = source_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing Shopee process config: {source}")
        payload = yaml.safe_load(source.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Shopee process config must be a mapping: {source}")
        target = target_dir / name
        target.write_text(
            yaml.safe_dump(
                build_public_process_config(payload),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        generated.append(target)
    return generated


def generate_public_bigseller_config(source: Path, output_dir: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Missing BigSeller process config: {source}")
    payload = yaml.safe_load(source.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"BigSeller process config must be a mapping: {source}")
    target = output_dir / "mabang_process" / "vietnam" / "config" / BIGSELLER_CONFIG_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            build_public_bigseller_config(payload),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return target


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    source_dir = project_root.parent / "superbrowser_process" / "main" / "shopee" / "config"
    output_dir = project_root / "build" / "public_runtime_config"
    generated = generate_public_runtime_configs(source_dir, output_dir)
    generated.append(
        generate_public_bigseller_config(
            project_root.parent / "mabang_process" / "vietnam" / "config" / BIGSELLER_CONFIG_NAME,
            output_dir,
        )
    )
    print(f"Generated {len(generated)} public runtime configs under {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
