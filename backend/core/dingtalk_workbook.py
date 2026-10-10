"""钉钉在线表格最小客户端。

提供读写钉钉工作簿 Sheet 的能力，专为网红寄样登记流程设计。
凭证通过 config/config.yaml 读取，不暴露给前端。
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
_USER_GET_URL = "https://oapi.dingtalk.com/topapi/v2/user/get"

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


class DingTalkCredentialsError(RuntimeError):
    """Raised when DingTalk app credentials are not configured."""


# ---------------------------------------------------------------------------
# 偶发错误重试
# ---------------------------------------------------------------------------

# 钉钉文档接口在高峰期会返回 ServiceUnavailable(503) 等临时错误；当前 SDK 版本
# （tea-util 0.3.x）的 RuntimeOptions 不支持自动重试，因此统一在这里重试：
# 最多尝试 DINGTALK_MAX_ATTEMPTS 次（首次调用 + 4 次重试），仅重试 5xx/限流/网络抖动，
# 业务类错误（参数错误、无权限、资源不存在）立即抛出，避免无谓等待。
DINGTALK_MAX_ATTEMPTS = 5
DINGTALK_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
_RETRYABLE_TOKENS = (
    "serviceunavailable", "service unavailable", "throttling", "temporary failure",
    "internalerror", "internal error", "server error", "gateway timeout",
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection refused", "remoteprotocolerror", "connectionpool", "econnreset",
)
_RETRYABLE_STATUS_RE = re.compile(r"(?<!\d)50[0-4](?!\d)")


def is_retryable_dingtalk_error(exc: BaseException) -> bool:
    """是否为可重试的临时错误（5xx、限流或网络抖动）。"""
    text = str(exc).casefold()
    if any(token in text for token in _RETRYABLE_TOKENS):
        return True
    return bool(_RETRYABLE_STATUS_RE.search(text))


def call_with_retry(
    operation,
    *,
    description: str = "钉钉接口",
    attempts: int = DINGTALK_MAX_ATTEMPTS,
    sleeper=time.sleep,
):
    """执行 ``operation()``，对可重试的临时错误最多尝试 ``attempts`` 次。"""
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - 需要按错误类型决定是否重试
            retryable = is_retryable_dingtalk_error(exc)
            if attempt >= attempts or not retryable:
                if retryable:
                    LOGGER.warning("%s：重试 %s 次后仍失败：%s", description, attempts, exc)
                raise
            delay = DINGTALK_RETRY_DELAYS[min(attempt - 1, len(DINGTALK_RETRY_DELAYS) - 1)]
            LOGGER.warning(
                "%s：遇到临时错误，%g 秒后重试（第 %s/%s 次）：%s",
                description, delay, attempt, attempts, exc,
            )
            _notify_retry(
                f"钉钉接口临时错误，{delay:g} 秒后重试（第 {attempt}/{attempts} 次）：{description}"
            )
            sleeper(delay)


# 可选的重试事件观察者：由业务 runner 注册，让「正在重试」出现在该任务的运行日志里。
# 桌面端任务串行执行，因此用模块级单一观察者即可。
_retry_observer: Callable[[str], None] | None = None


def set_retry_observer(observer: Callable[[str], None] | None) -> None:
    """注册重试事件观察者（传 None 取消）。"""
    global _retry_observer
    _retry_observer = observer


def _notify_retry(message: str) -> None:
    observer = _retry_observer
    if observer is None:
        return
    try:
        observer(message)
    except Exception:  # noqa: BLE001 - 观察者自身失败不得影响重试流程
        LOGGER.debug("重试事件观察者执行失败", exc_info=True)


# ---------------------------------------------------------------------------
# 凭证读取
# ---------------------------------------------------------------------------

def _config_file_candidates() -> list[Path]:
    """获取可能的 config/config.yaml 路径。"""
    import os

    candidates: list[Path] = []
    env_path = os.getenv("HQYL_CONFIG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[2]
    candidates.append(base / "config" / "config.yaml")
    return candidates


def load_dingtalk_credentials() -> dict[str, str]:
    """从 config.yaml 读取钉钉凭证。"""
    for cfg_path in _config_file_candidates():
        if not cfg_path.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        dingtalk = data.get("dingtalk", {}) if isinstance(data, dict) else {}
        if not isinstance(dingtalk, dict):
            continue
        app_rpa = dingtalk.get("app", {}).get("rpa", {})
        doc_config = dingtalk.get("doc_config", {})
        if not isinstance(app_rpa, dict):
            app_rpa = {}
        if not isinstance(doc_config, dict):
            doc_config = {}
        creds = {
            "app_key": str(app_rpa.get("app_key") or doc_config.get("app_key") or ""),
            "app_secret": str(app_rpa.get("app_secret") or doc_config.get("app_secret") or ""),
            "user_id": str(app_rpa.get("user_id") or doc_config.get("app_userid") or doc_config.get("user_id") or ""),
        }
        if any(creds.values()):
            return creds
    return {}


def get_credentials() -> tuple[str, str, str]:
    """获取 (app_key, app_secret, user_id)，优先从配置文件读取。"""
    import os

    creds = load_dingtalk_credentials()
    app_key = creds.get("app_key") or os.getenv("DINGTALK_APP_KEY", "")
    app_secret = creds.get("app_secret") or os.getenv("DINGTALK_APP_SECRET", "")
    user_id = creds.get("user_id") or os.getenv("DINGTALK_USER_ID", "")
    missing = [
        name for name, value in (
            ("app_key", app_key),
            ("app_secret", app_secret),
            ("user_id", user_id),
        )
        if not value
    ]
    if missing:
        raise DingTalkCredentialsError(
            "缺少钉钉应用凭证，请在 config/config.yaml 的 dingtalk.app.rpa 中配置 "
            f"{', '.join(missing)}，或设置 DINGTALK_APP_KEY/DINGTALK_APP_SECRET/DINGTALK_USER_ID 环境变量"
        )
    return app_key, app_secret, user_id


# ---------------------------------------------------------------------------
# Token 管理
# ---------------------------------------------------------------------------

@dataclass
class DingTalkToken:
    access_token: str = ""
    union_id: str = ""
    expires_at: float = 0.0


_token_cache = DingTalkToken()


def get_access_token(app_key: str, app_secret: str) -> str:
    """获取钉钉 access_token（对 503 等临时错误重试）。"""
    global _token_cache
    now = time.time()
    if _token_cache.access_token and _token_cache.expires_at > now + 60:
        return _token_cache.access_token

    def fetch() -> dict[str, Any]:
        resp = httpx.get(_TOKEN_URL, params={"appkey": app_key, "appsecret": app_secret}, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"获取钉钉 access_token 失败（HTTP {resp.status_code}）")
        data = resp.json()
        errcode = data.get("errcode", -1)
        if errcode != 0:
            raise RuntimeError(f"获取钉钉 access_token 失败：{data.get('errmsg', errcode)}")
        return data

    data = call_with_retry(fetch, description="获取钉钉 access_token")
    token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 7200)
    _token_cache.access_token = token
    _token_cache.expires_at = now + expires_in
    return token


def get_union_id(access_token: str, user_id: str) -> str:
    """获取用户的 union_id（对 503 等临时错误重试）。"""
    global _token_cache
    if _token_cache.union_id:
        return _token_cache.union_id

    def fetch() -> str:
        resp = httpx.post(
            _USER_GET_URL,
            params={"access_token": access_token},
            json={"userid": user_id, "language": "zh_CN"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"读取钉钉操作人失败（HTTP {resp.status_code}）")
        data = resp.json()
        union_id = str((data.get("result") or {}).get("unionid") or "")
        if data.get("errcode") != 0 or not union_id:
            raise RuntimeError(f"获取 union_id 失败：{data}")
        return union_id

    union_id = call_with_retry(fetch, description="获取钉钉 union_id")
    _token_cache.union_id = union_id
    return union_id


# ---------------------------------------------------------------------------
# Sheet 操作（通过 alibabacloud_dingtalk SDK）
# ---------------------------------------------------------------------------

def _get_sdk_client(app_key: str | None = None, app_secret: str | None = None):
    """延迟导入 SDK 客户端。"""
    from alibabacloud_dingtalk.doc_1_0.client import Client as DocClient
    from alibabacloud_tea_openapi import models as open_api_models

    if not (app_key and app_secret):
        app_key, app_secret, _ = get_credentials()
    config = open_api_models.Config()
    config.protocol = "https"
    config.region_id = "central"
    config.access_key_id = app_key
    config.access_key_secret = app_secret
    return DocClient(config)


def _runtime_options():
    from alibabacloud_tea_util import models as util_models

    return util_models.RuntimeOptions()


def _model_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_map"):
        mapped = value.to_map()
        return mapped if isinstance(mapped, dict) else {}
    result: dict[str, Any] = {}
    for name in ("id", "name", "row_count", "column_count", "last_non_empty_row", "last_non_empty_column"):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _normalize_sheet_meta(value: Any) -> dict[str, Any]:
    meta = _model_to_dict(value)
    aliases = {
        "row_count": "rowCount",
        "column_count": "columnCount",
        "last_non_empty_row": "lastNonEmptyRow",
        "last_non_empty_column": "lastNonEmptyColumn",
    }
    for source, target in aliases.items():
        if source in meta and target not in meta:
            meta[target] = meta[source]
    return meta


def get_all_sheets(
    access_token: str,
    union_id: str,
    doc_id: str,
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> list[dict[str, Any]]:
    """获取工作簿所有 Sheet 列表。"""
    client = _get_sdk_client(app_key, app_secret)
    from alibabacloud_dingtalk.doc_1_0 import models as doc_models

    headers = doc_models.GetAllSheetsHeaders(x_acs_dingtalk_access_token=access_token)
    request = doc_models.GetAllSheetsRequest(operator_id=union_id)
    resp = call_with_retry(
        lambda: client.get_all_sheets_with_options(doc_id, request, headers, _runtime_options()),
        description="读取 Sheet 列表",
    )
    return [_normalize_sheet_meta(item) for item in (resp.body.value or [])]


def get_sheet_meta_by_name(
    access_token: str,
    union_id: str,
    doc_id: str,
    sheet_name: str,
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> dict[str, Any] | None:
    """按名称查找 Sheet 元数据。"""
    sheets = get_all_sheets(access_token, union_id, doc_id, app_key=app_key, app_secret=app_secret)
    for s in sheets:
        name = str(s.get("name") or "").strip()
        if name == sheet_name.strip():
            sheet_id = str(s.get("id") or "").strip()
            if not sheet_id:
                return s
            client = _get_sdk_client(app_key, app_secret)
            from alibabacloud_dingtalk.doc_1_0 import models as doc_models

            headers = doc_models.GetSheetHeaders(x_acs_dingtalk_access_token=access_token)
            request = doc_models.GetSheetRequest(operator_id=union_id)
            resp = call_with_retry(
                lambda: client.get_sheet_with_options(doc_id, sheet_id, request, headers, _runtime_options()),
                description=f"读取 Sheet 元数据（{name}）",
            )
            meta = _normalize_sheet_meta(resp.body)
            meta.setdefault("id", sheet_id)
            meta.setdefault("name", name)
            return meta
    return None


def read_sheet_range(
    access_token: str,
    union_id: str,
    doc_id: str,
    sheet_id: str,
    cell_range: str,
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
) -> list[list[Any]]:
    """读取 Sheet 指定范围的值。"""
    client = _get_sdk_client(app_key, app_secret)
    from alibabacloud_dingtalk.doc_1_0 import models as doc_models

    headers = doc_models.GetRangeHeaders(x_acs_dingtalk_access_token=access_token)
    request = doc_models.GetRangeRequest(operator_id=union_id, select="values")
    resp = call_with_retry(
        lambda: client.get_range_with_options(doc_id, sheet_id, cell_range, request, headers, _runtime_options()),
        description=f"读取 {cell_range}",
    )
    return resp.body.values or []


def update_sheet_range(
    access_token: str,
    union_id: str,
    doc_id: str,
    sheet_id: str,
    cell_range: str,
    values: list[list[Any]],
    *,
    number_format: str = "General",
    app_key: str | None = None,
    app_secret: str | None = None,
) -> None:
    """写入 Sheet 指定范围的值（对 503 等临时错误重试）。

    写入重试的前提是钉钉接口是幂等覆盖写入：同一 range 重复写同样的值不会产生副作用。
    """
    client = _get_sdk_client(app_key, app_secret)
    from alibabacloud_dingtalk.doc_1_0 import models as doc_models

    headers = doc_models.UpdateRangeHeaders(x_acs_dingtalk_access_token=access_token)
    request = doc_models.UpdateRangeRequest(
        operator_id=union_id, values=values, number_format=number_format,
    )
    call_with_retry(
        lambda: client.update_range_with_options(doc_id, sheet_id, cell_range, request, headers, _runtime_options()),
        description=f"写入 {cell_range}",
    )


def append_sheet_rows(
    access_token: str,
    union_id: str,
    doc_id: str,
    sheet_id: str,
    values: list[list[Any]],
    *,
    number_format: str = "@",
    app_key: str | None = None,
    app_secret: str | None = None,
) -> None:
    """向 Sheet 追加行数据。"""
    if not values:
        return
    client = _get_sdk_client(app_key, app_secret)
    from alibabacloud_dingtalk.doc_1_0 import models as doc_models

    del number_format  # AppendRowsRequest 不支持单独设置格式，保留参数兼容旧调用。
    headers = doc_models.AppendRowsHeaders(x_acs_dingtalk_access_token=access_token)
    request = doc_models.AppendRowsRequest(
        operator_id=union_id,
        values=[["" if cell is None else str(cell) for cell in row] for row in values],
    )
    call_with_retry(
        lambda: client.append_rows_with_options(doc_id, sheet_id, request, headers, _runtime_options()),
        description="追加 Sheet 行",
    )
