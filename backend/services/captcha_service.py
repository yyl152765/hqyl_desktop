from __future__ import annotations

from typing import Any

import requests


TTSHITU_API_URL = "http://api.ttshitu.com"


class CaptchaServiceError(RuntimeError):
    """Raised when the captcha provider rejects or cannot answer a request."""


def query_ttshitu_account_info(
    username: str,
    password: str,
    timeout: int = 30,
) -> dict[str, Any]:
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        raise ValueError("请输入打码平台账号")
    if not password:
        raise ValueError("请输入打码平台密码")

    try:
        response = requests.get(
            f"{TTSHITU_API_URL}/queryAccountInfo.json",
            params={"username": username, "password": password},
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
    except requests.Timeout as exc:
        raise CaptchaServiceError("打码平台连接超时，请稍后重试") from exc
    except requests.RequestException as exc:
        # Request exceptions may include the full GET URL. Do not echo it because
        # this provider's official endpoint carries the password in query params.
        raise CaptchaServiceError("打码平台连接失败，请检查网络后重试") from exc
    except ValueError as exc:
        raise CaptchaServiceError("打码平台返回了无法解析的数据") from exc

    if not isinstance(result, dict):
        raise CaptchaServiceError("打码平台返回格式不正确")
    if not result.get("success"):
        message = str(result.get("message") or "查询失败").strip()
        raise CaptchaServiceError(f"打码平台查询失败：{message}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise CaptchaServiceError("打码平台未返回账户信息")
    return data
