import time
from typing import Any, Dict, Optional

import requests


class SmsOtpTaskError(Exception):
    pass


def _request_json(method: str, url: str, **kwargs) -> Dict[str, Any]:
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise SmsOtpTaskError(f"{exc}; response={response.text}") from exc
    data = response.json()
    if data.get("ok") is False:
        raise SmsOtpTaskError(str(data))
    return data


def create_sms_otp_task(
    base_url: str,
    task_id: str,
    system_name: str,
    phone: str,
    user_id: str,
    requester: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body = {
        "task_id": task_id,
        "system_name": system_name,
        "phone": phone,
        "user_id": user_id,
        "requester": requester,
    }
    if metadata:
        body["metadata"] = metadata
    return _request_json("POST", f"{base_url.rstrip('/')}/api/sms-otp/request", json=body)


def get_sms_otp_task(base_url: str, task_id: str) -> Dict[str, Any]:
    return _request_json("GET", f"{base_url.rstrip('/')}/api/sms-otp/{task_id}")


def mark_sms_sent(base_url: str, task_id: str) -> Dict[str, Any]:
    return _request_json("POST", f"{base_url.rstrip('/')}/api/sms-otp/{task_id}/mark-sent")


def wait_for_sms_otp_code(
    base_url: str,
    task_id: str,
    timeout: int = 300,
    poll_interval: int = 5,
) -> str:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        data = get_sms_otp_task(base_url, task_id)
        task = data.get("task") or {}
        last_status = task.get("status", "")
        otp_code = task.get("otp_code")
        if last_status == "code_received" and otp_code:
            return str(otp_code)
        if last_status in {"cancelled", "expired", "failed", "finished"}:
            raise SmsOtpTaskError(f"SMS OTP task ended with status={last_status}: {task}")
        time.sleep(poll_interval)
    raise SmsOtpTaskError(f"SMS OTP task timeout, last_status={last_status}, task_id={task_id}")
