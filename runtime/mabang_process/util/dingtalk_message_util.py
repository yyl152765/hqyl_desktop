# -*- coding: utf-8 -*-
import sys, os
from collections.abc import Sequence
from pathlib import Path

process_config_path = os.path.dirname(__file__)
config_path = os.path.dirname(os.path.dirname(__file__))
sys.path.append(config_path)
from alibabacloud_dingtalk.oauth2_1_0.client import Client as DingtalkClient
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalk_models
import requests
import hashlib, os
from alibabacloud_dingtalk.storage_1_0.client import Client as StorageClient
from alibabacloud_dingtalk.storage_1_0 import models as storage_models
from alibabacloud_tea_util import models as util_models
import yaml
from alibabacloud_tea_openapi import models as open_api_models
import time
import hmac
import base64
import urllib.parse

HTTP_TIMEOUT = (5, 30)
HQYL_MARKERS = (
    "HQYL_ATTEMPT_ID",
    "HQYL_JOB_DIR",
    "HQYL_OUTPUT_DIR",
    "HQYL_INPUT_FILE",
    "HQYL_PARAMETERS_JSON",
    "HQYL_ASSETS_JSON",
)


def _is_hqyl_runtime():
    return any(name in os.environ for name in HQYL_MARKERS)


def _runtime_options():
    return util_models.RuntimeOptions(
        autoretry=False,
        ignore_ssl=False,
        connect_timeout=5_000,
        read_timeout=30_000,
    )


def clean_text(value) -> str:
    if value is None:
        return ''
    return str(value).strip()


def _load_default_dingtalk_config():
    if _is_hqyl_runtime():
        return {}, {}
    config_file = os.path.join(config_path, 'config', 'config.yaml')
    if not os.path.isfile(config_file):
        return {}, {}
    with open(config_file, 'r', encoding='utf-8') as config:
        cfg = yaml.safe_load(config) or {}
    return cfg, (((cfg.get('dingtalk') or {}).get('app') or {}).get('rpa') or {})


def _merge_non_empty_config(base_config, override_config):
    merged = dict(base_config or {})
    for key, value in (override_config or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    return merged


def _normalize_group_file_conflict_strategy(conflict_strategy: str = None) -> str:
    normalized = clean_text(conflict_strategy)
    if not normalized:
        return "AUTO_RENAME"

    lowered = normalized.lower()
    mapping = {
        "自动重命名": "AUTO_RENAME",
        "auto_rename": "AUTO_RENAME",
        "auto rename": "AUTO_RENAME",
        "autorename": "AUTO_RENAME",
        "覆盖": "OVERWRITE",
        "overwrite": "OVERWRITE",
        "replace": "OVERWRITE",
    }
    if normalized in mapping:
        return mapping[normalized]
    if lowered in mapping:
        return mapping[lowered]

    uppered = normalized.upper()
    if uppered in {"AUTO_RENAME", "OVERWRITE"}:
        return uppered
    return "AUTO_RENAME"

#获取token
def get_access_token(app_key: str, app_secret: str):
    # 初始化 client
    config = open_api_models.Config(protocol='https', region_id='central')
    client = DingtalkClient(config)

    # 请求 access_token
    req = dingtalk_models.GetAccessTokenRequest(app_key=app_key, app_secret=app_secret)
    resp = client.get_access_token_with_options(req, {}, _runtime_options())

    return resp.body.access_token, resp.body.expire_in
#获取用户信息
def get_user_info(user_id, access_token):
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    data = {
        "userid": user_id,
        "language": "zh_CN"
    }
    response = requests.post(
        url,
        params={"access_token": access_token},
        data=data,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("DingTalk user API returned an unexpected response")
    return payload

# ===== 函数 =====
def calc_md5(file_path):
    """计算文件 MD5"""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5.update(chunk)
    return md5.hexdigest()


def create_client():
    """初始化钉钉存储客户端"""
    config = open_api_models.Config(protocol='https', region_id='central')
    return StorageClient(config)


def calc_md5(file_path):
    """计算文件 md5"""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5.update(chunk)
    return md5.hexdigest()

# 第一步
def _is_retryable_dingtalk_error(exc: Exception) -> bool:
    message = str(exc)
    retryable_markers = (
        'dentryStoreError',
        'file store driver error',
        'systemError',
        'statusCode\': 500',
        'statusCode": 500',
        'code: 500',
        'InternalError',
        'ServiceUnavailable',
        'timeout',
        'Timeout',
        'Connection',
    )
    return any(marker in message for marker in retryable_markers)


def get_upload_info(client, access_token, space_id, union_id, parent_id, file_path, max_retries=3):
    """获取上传地址"""
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_md5 = calc_md5(file_path)

    headers = storage_models.GetFileUploadInfoHeaders(
        x_acs_dingtalk_access_token=access_token
    )
    pre_check = storage_models.GetFileUploadInfoRequestOptionPreCheckParam(
        md_5=file_md5, size=file_size, parent_id=parent_id, name=file_name
    )
    option = storage_models.GetFileUploadInfoRequestOption(
        storage_driver='DINGTALK',
        pre_check_param=pre_check,
        prefer_region='ZHANGJIAKOU',
        prefer_intranet=False
    )
    request = storage_models.GetFileUploadInfoRequest(
        union_id=union_id,
        protocol='HEADER_SIGNATURE',
        multipart=False,
        option=option
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.get_file_upload_info_with_options(
                space_id, request, headers, _runtime_options()
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries or not _is_retryable_dingtalk_error(exc):
                raise
            wait_seconds = min(2 ** (attempt - 1), 8)
            print(f"DingTalk upload info failed, retrying in {wait_seconds}s ({attempt}/{max_retries}): {exc}")
            time.sleep(wait_seconds)
    else:
        raise last_error
    return resp.to_map()
# 第二步
def upload_file_with_header_signature(resource_url: str, headers: dict, file_path: str):
    """
    使用 HEADER_SIGNATURE 上传文件到钉盘 OSS

    :param resource_url: applyUpload 返回的 resourceUrls[0]
    :param headers: applyUpload 返回的 headerSignatureInfo.headers（包含 Authorization 和 x-oss-date）
    :param file_path: 本地文件路径
    """

    try:
        with open(file_path, "rb") as f:
            response = requests.put(resource_url, headers=headers, data=f, timeout=30)
        response.raise_for_status()
        print("上传成功")

    except requests.RequestException:
        raise RuntimeError("DingTalk file upload failed") from None
def commit_file(
    client,
    access_token: str,
    space_id: str,
    union_id: str,
    upload_key: str,
    file_name: str,
    parent_id: str = "0",
    file_size: int = None,
    conflict_strategy: str = "AUTO_RENAME",
    app_properties: list = None
):
    """
    提交文件上传完成，生成云盘文件记录
    """
    from alibabacloud_dingtalk.storage_1_0 import models as storage_models
    from alibabacloud_tea_util import models as util_models

    # Headers
    headers = storage_models.CommitFileHeaders(
        x_acs_dingtalk_access_token=access_token
    )

    # Option
    option = storage_models.CommitFileRequestOption(
        size=file_size,
        conflict_strategy=conflict_strategy,
        app_properties=[]
    )

    if app_properties:
        for prop in app_properties[:3]:
            option.app_properties.append(
                storage_models.CommitFileRequestOptionAppProperties(
                    name=prop['name'],
                    value=prop['value'],
                    visibility=prop['visibility']
                )
            )

    # Request
    request = storage_models.CommitFileRequest(
        upload_key=upload_key,
        name=file_name,
        parent_id=parent_id,
        option=option,
        union_id=union_id
    )

    # 真正调用提交接口
    try:
        response = client.commit_file_with_options(
            space_id,
            request,
            headers,
            _runtime_options()
        )
        return response.body
    except Exception as e:
        raise Exception(f"提交文件失败: {e}")

def send_ding_file(access_token: str, space_id: str, dentry_id: str, union_id: str):
    from alibabacloud_dingtalk.conv_file_1_0.client import Client as ConvFileClient
    from alibabacloud_dingtalk.conv_file_1_0 import models as convfile_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models
    from alibabacloud_tea_util.client import Client as UtilClient
    """
    发送钉钉云盘中已存在的文件给指定用户
    :param access_token: 钉钉 access_token
    :param space_id: 云盘空间ID
    :param dentry_id: 文件ID
    :param union_id: 接收用户 union_id
    """
    # 初始化 client
    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    client = ConvFileClient(config)

    # 构建请求
    headers = convfile_models.SendByAppHeaders()
    headers.x_acs_dingtalk_access_token = access_token

    request = convfile_models.SendByAppRequest(
        union_id=union_id,
        space_id=space_id,
        dentry_id=dentry_id
    )

    # 调用发送接口
    try:
        client.send_by_app_with_options(request, headers, _runtime_options())
        print(f"文件 {dentry_id} 已发送给用户 {union_id}")
    except Exception as err:
            print(f"发送失败: {err}")


def send_ding_file_strict(
    access_token: str,
    space_id: str,
    dentry_id: str,
    union_id: str,
):
    """Send an app file message and propagate any delivery failure.

    Existing callers keep the legacy best-effort ``send_ding_file`` behavior.
    HQYL flows that report delivery success must use this strict variant so a
    failed DingTalk API call makes the Job fail instead of being logged and
    silently ignored.
    """

    from alibabacloud_dingtalk.conv_file_1_0.client import Client as ConvFileClient
    from alibabacloud_dingtalk.conv_file_1_0 import models as convfile_models
    from alibabacloud_tea_openapi import models as open_api_models

    config = open_api_models.Config(protocol="https", region_id="central")
    client = ConvFileClient(config)
    headers = convfile_models.SendByAppHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = convfile_models.SendByAppRequest(
        union_id=union_id,
        space_id=space_id,
        dentry_id=dentry_id,
    )
    return client.send_by_app_with_options(request, headers, _runtime_options())


def _strict_dentry_id(commit_result) -> str:
    dentry = getattr(commit_result, "dentry", None)
    dentry_id = clean_text(getattr(dentry, "id", None))
    if not dentry_id and isinstance(commit_result, dict):
        raw_dentry = commit_result.get("dentry") or {}
        if isinstance(raw_dentry, dict):
            dentry_id = clean_text(raw_dentry.get("id"))
    if not dentry_id:
        raise RuntimeError("DingTalk file commit returned no dentry id")
    return dentry_id


class DingTalkDeliveryError(RuntimeError):
    """Authored delivery failure carrying a whitelisted, credential-free detail."""


def _safe_delivery_cause(error) -> str:
    """Summarise the SDK/HTTP cause with the approved diagnostic whitelist.

    ``util.dingtalk_doc_api.safe_exception_summary`` unwraps Tea/Darabonba
    exception chains and emits only known API codes, HTTP status, request IDs
    and network classes; unknown codes and raw messages stay hidden. The detail
    therefore names the failing stage without exposing responses or IDs.
    """
    try:
        from util.dingtalk_doc_api import safe_exception_summary
    except Exception:
        return type(error).__name__
    return safe_exception_summary(error)


def _delivery_error(stage: str, error, **context) -> DingTalkDeliveryError:
    parts = [f"阶段={stage}"]
    parts.extend(f"{key}={clean_text(value)}" for key, value in context.items())
    parts.append(_safe_delivery_cause(error))
    return DingTalkDeliveryError("; ".join(parts))


def delivery_failure_detail(error) -> str:
    """Render the log-safe detail for a failed delivery attempt.

    Only errors raised by this module carry authored context (stage, file,
    recipient). Any other exception is reduced to the whitelisted summary so
    remote response text can never reach a Job log.
    """
    if isinstance(error, DingTalkDeliveryError):
        return str(error)
    return _safe_delivery_cause(error)


def send_files_to_users_strict(
    files,
    *,
    app_key: str,
    app_secret: str,
    sender_user_id: str,
    space_id: str,
    parent_id: str = "0",
    recipients,
):
    """Upload each file once and strictly send it to every named recipient.

    ``recipients`` is a sequence of ``(display_name, user_id)`` pairs. All
    recipient union IDs are resolved before the first upload, preventing an
    invalid recipient from causing a partially uploaded delivery batch. The
    returned records contain only file-level metadata and never user IDs.
    """

    required = {
        "app_key": app_key,
        "app_secret": app_secret,
        "sender_user_id": sender_user_id,
        "space_id": space_id,
    }
    missing = [name for name, value in required.items() if not clean_text(value)]
    if missing:
        raise ValueError("DingTalk file config missing: " + ", ".join(missing))
    if isinstance(files, (str, bytes, os.PathLike)):
        raise ValueError("files must be a sequence of file paths")
    if isinstance(recipients, (str, bytes)) or not isinstance(recipients, Sequence):
        raise ValueError("recipients must be a sequence of (name, user_id) pairs")

    normalized_files = []
    seen_files = set()
    for raw_path in files:
        file_path = Path(raw_path).expanduser().resolve(strict=True)
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            raise ValueError(f"DingTalk delivery file is empty: {file_path.name}")
        if file_path in seen_files:
            raise ValueError(f"DingTalk delivery file is duplicated: {file_path.name}")
        seen_files.add(file_path)
        normalized_files.append(file_path)
    if not normalized_files:
        raise ValueError("files must not be empty")

    normalized_recipients = []
    seen_user_ids = set()
    for recipient in recipients:
        if not isinstance(recipient, Sequence) or isinstance(recipient, (str, bytes)):
            raise ValueError("each recipient must be a (name, user_id) pair")
        if len(recipient) != 2:
            raise ValueError("each recipient must be a (name, user_id) pair")
        name = clean_text(recipient[0])
        user_id = clean_text(recipient[1])
        if not name or not user_id:
            raise ValueError("recipient name and user_id must not be empty")
        if user_id in seen_user_ids:
            raise ValueError("multiple recipients resolve to the same DingTalk user")
        seen_user_ids.add(user_id)
        normalized_recipients.append((name, user_id))
    if not normalized_recipients:
        raise ValueError("recipients must not be empty")

    try:
        access_token, _ = get_access_token(clean_text(app_key), clean_text(app_secret))
    except Exception as exc:
        raise _delivery_error("获取访问令牌", exc) from None
    try:
        sender_union_id = get_union_id(clean_text(sender_user_id), access_token)
    except Exception as exc:
        raise _delivery_error("解析发送人", exc) from None

    resolved_recipients = []
    seen_union_ids = set()
    for name, user_id in normalized_recipients:
        try:
            union_id = get_union_id(user_id, access_token)
        except Exception as exc:
            raise _delivery_error("解析收件人", exc, recipient=name) from None
        if union_id in seen_union_ids:
            # Names stay loggable; union IDs stay out of every message.
            raise DingTalkDeliveryError("阶段=解析收件人; 多个收件人解析为同一钉钉用户")
        seen_union_ids.add(union_id)
        resolved_recipients.append((name, union_id))

    target_space_id = clean_text(space_id)
    target_parent_id = clean_text(parent_id) or "0"
    delivered = []
    for file_path in normalized_files:
        try:
            commit_result = upload_file_to_space(
                str(file_path),
                access_token=access_token,
                union_id=sender_union_id,
                space_id=target_space_id,
                parent_id=target_parent_id,
                conflict_strategy="AUTO_RENAME",
            )
            dentry_id = _strict_dentry_id(commit_result)
        except Exception as exc:
            raise _delivery_error("上传文件", exc, file=file_path.name) from None

        for recipient_name, recipient_union_id in resolved_recipients:
            try:
                send_ding_file_strict(
                    access_token,
                    target_space_id,
                    dentry_id,
                    recipient_union_id,
                )
            except Exception as exc:
                raise _delivery_error("发送文件", exc, file=file_path.name,
                                      recipient=recipient_name) from None
        delivered.append(
            {
                "name": file_path.name,
                "delivery_count": len(resolved_recipients),
            }
        )
        # Partial-progress record for the operator deciding how to re-send.
        print(f"钉钉文件已上传并发送: {file_path.name} -> {len(resolved_recipients)}人",
              file=sys.stderr, flush=True)
    return delivered


def get_union_id(user_id: str, access_token: str) -> str:
    user_info = get_user_info(str(user_id), access_token)
    result = user_info.get("result") or {}
    union_id = result.get("unionid")
    if not union_id:
        raise ValueError(f"failed to resolve union id for user_id={user_id}: {user_info}")
    return str(union_id)


def get_conversation_space(access_token: str, open_conversation_id: str, union_id: str):
    from alibabacloud_dingtalk.conv_file_1_0.client import Client as ConvFileClient
    from alibabacloud_dingtalk.conv_file_1_0 import models as convfile_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    client = ConvFileClient(config)

    headers = convfile_models.GetSpaceHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = convfile_models.GetSpaceRequest(
        union_id=union_id,
        open_conversation_id=open_conversation_id
    )
    response = client.get_space_with_options(request, headers, _runtime_options())
    return response.body


def resolve_open_conversation_id(access_token: str, conversation_id: str) -> str:
    from alibabacloud_dingtalk.im_1_0.client import Client as ImClient
    from alibabacloud_dingtalk.im_1_0 import models as im_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

    normalized_conversation_id = clean_text(conversation_id)
    if not normalized_conversation_id:
        raise ValueError('conversation_id is required')

    if normalized_conversation_id.startswith('cid'):
        return normalized_conversation_id

    if normalized_conversation_id.startswith('chat'):
        config = open_api_models.Config()
        config.protocol = 'https'
        config.region_id = 'central'
        client = ImClient(config)
        headers = im_models.ChatIdToOpenConversationIdHeaders()
        headers.x_acs_dingtalk_access_token = access_token
        response = client.chat_id_to_open_conversation_id_with_options(
            normalized_conversation_id,
            headers,
            _runtime_options()
        )
        body = getattr(response, 'body', None)
        open_conversation_id = clean_text(getattr(body, 'open_conversation_id', None))
        if not open_conversation_id:
            raise ValueError(f'failed to resolve openConversationId from chatId={normalized_conversation_id}')
        return open_conversation_id

    return normalized_conversation_id


def send_conversation_file(
    access_token: str,
    space_id: str,
    dentry_id: str,
    union_id: str,
    open_conversation_id: str
):
    from alibabacloud_dingtalk.conv_file_1_0.client import Client as ConvFileClient
    from alibabacloud_dingtalk.conv_file_1_0 import models as convfile_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    client = ConvFileClient(config)

    headers = convfile_models.SendHeaders()
    headers.x_acs_dingtalk_access_token = access_token
    request = convfile_models.SendRequest(
        union_id=union_id,
        space_id=space_id,
        dentry_id=dentry_id,
        open_conversation_id=open_conversation_id
    )
    return client.send_with_options(request, headers, _runtime_options())


def _is_param_error(exc: Exception) -> bool:
    message = str(exc)
    return 'paramError' in message or 'param is invalid' in message


def _build_group_file_upload_candidates(
    *,
    default_space_id: str,
    default_parent_id: str,
    resolved_space_id: str = '',
):
    candidates = []

    def add_candidate(space_id: str, parent_id: str):
        normalized_space_id = str(space_id or '').strip()
        normalized_parent_id = str(parent_id or '0').strip() or '0'
        if not normalized_space_id:
            return
        candidate = (normalized_space_id, normalized_parent_id)
        if candidate not in candidates:
            candidates.append(candidate)

    if resolved_space_id:
        add_candidate(resolved_space_id, '0' if resolved_space_id != default_space_id else default_parent_id)
        add_candidate(resolved_space_id, '0')

    add_candidate(default_space_id, default_parent_id)
    add_candidate(default_space_id, '0')
    return candidates


def upload_file_to_space(
    file_path: str,
    *,
    access_token: str,
    union_id: str,
    space_id: str,
    parent_id: str,
    conflict_strategy: str = "AUTO_RENAME"
):
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    client = create_client()
    info = get_upload_info(client, access_token, str(space_id), union_id, str(parent_id), file_path)
    resource_url = info['body']["headerSignatureInfo"]["resourceUrls"][0]
    headers = info['body']["headerSignatureInfo"]["headers"]
    upload_key = info['body']["uploadKey"]
    upload_file_with_header_signature(resource_url, headers, file_path)
    return commit_file(
        client,
        access_token,
        str(space_id),
        union_id,
        upload_key,
        file_name,
        str(parent_id),
        file_size,
        conflict_strategy=conflict_strategy,
        app_properties=None
    )


def send_group_file_dingtalk(
    file_path,
    *,
    sender_user_id: str = None,
    space_id: str = None,
    parent_id: str = None,
    open_conversation_id: str,
    cf=None,
    conflict_strategy: str = "AUTO_RENAME",
    resolve_conversation_space: bool = False
):
    if cf is None:
        _, cf = _load_default_dingtalk_config()
    if not cf:
        raise ValueError('DingTalk app configuration is required')

    sender_user_id = str(sender_user_id or cf.get('user_id') or '').strip()
    space_id = str(space_id or cf.get('space_id') or '').strip()
    parent_id = str(parent_id or cf.get('parent_id') or '0').strip() or '0'
    if not sender_user_id:
        raise ValueError('sender_user_id is required')
    if not space_id and not resolve_conversation_space:
        raise ValueError('space_id is required')

    token, expire = get_access_token(cf['app_key'], cf['app_secret'])
    sender_union_id = get_union_id(str(sender_user_id), token)
    open_conversation_id = resolve_open_conversation_id(token, open_conversation_id)

    resolved_space_id = ''
    if resolve_conversation_space:
        try:
            space_body = get_conversation_space(token, open_conversation_id, sender_union_id)
            if space_body and getattr(space_body, "space", None) and getattr(space_body.space, "space_id", None):
                resolved_space_id = str(space_body.space.space_id).strip()
        except Exception as exc:
            print(f"获取群空间失败，继续使用配置空间: {exc}")

    if not space_id and not resolved_space_id:
        raise ValueError('space_id is required when conversation space cannot be resolved')

    upload_candidates = _build_group_file_upload_candidates(
        default_space_id=space_id,
        default_parent_id=parent_id,
        resolved_space_id=resolved_space_id,
    )

    last_error = None
    for candidate_index, (target_space_id, target_parent_id) in enumerate(upload_candidates, start=1):
        try:
            commit_result = upload_file_to_space(
                str(file_path),
                access_token=token,
                union_id=sender_union_id,
                space_id=target_space_id,
                parent_id=target_parent_id,
                conflict_strategy=conflict_strategy,
            )
            dentry_id = commit_result.dentry.id
            send_result = send_conversation_file(
                token,
                target_space_id,
                dentry_id,
                sender_union_id,
                open_conversation_id
            )
            return {
                "space_id": target_space_id,
                "parent_id": target_parent_id,
                "dentry_id": dentry_id,
                "union_id": sender_union_id,
                "attempt": candidate_index,
                "send_result": send_result.to_map() if hasattr(send_result, "to_map") else send_result,
            }
        except Exception as exc:
            last_error = exc
            is_last_candidate = candidate_index == len(upload_candidates)
            if is_last_candidate or not _is_param_error(exc):
                raise
            print(f"群文件发送参数异常，尝试回退空间配置后重试: {exc}")

    raise last_error


def send_local_file_to_group_conversation(
    local_file_path,
    *,
    dingtalk_instance=None,
    sender_user_id: str = None,
    space_id: str = None,
    parent_id: str = None,
    open_conversation_id: str = None,
    chat_id: str = None,
    conflict_strategy: str = "AUTO_RENAME",
    resolve_conversation_space: bool = False,
    cf=None,
):
    """
    影刀式群文件发送：
    连接对象 + 空间ID + 父目录ID + 群会话ID + 本地文件路径 + 同名策略
    """
    default_cf = {}
    if cf is None and dingtalk_instance is None:
        _, default_cf = _load_default_dingtalk_config()
    resolved_cf = _merge_non_empty_config(default_cf, cf)
    resolved_cf = _merge_non_empty_config(resolved_cf, dingtalk_instance)

    open_conversation_id = clean_text(open_conversation_id or chat_id)
    sender_user_id = clean_text(sender_user_id or resolved_cf.get('user_id'))
    space_id = clean_text(space_id or resolved_cf.get('space_id'))
    parent_id = clean_text(parent_id or resolved_cf.get('parent_id') or '0') or '0'
    local_file_path = clean_text(local_file_path)
    if not local_file_path:
        raise ValueError('local_file_path is required')
    if not open_conversation_id:
        raise ValueError('open_conversation_id is required')
    if not sender_user_id:
        raise ValueError('sender_user_id is required')
    if not os.path.isfile(local_file_path):
        raise FileNotFoundError(local_file_path)

    if not space_id and not resolve_conversation_space:
        raise ValueError('space_id is required when resolve_conversation_space is False')

    return send_group_file_dingtalk(
        local_file_path,
        sender_user_id=sender_user_id,
        space_id=space_id,
        parent_id=parent_id,
        open_conversation_id=open_conversation_id,
        cf=resolved_cf,
        conflict_strategy=_normalize_group_file_conflict_strategy(conflict_strategy),
        resolve_conversation_space=resolve_conversation_space,
    )


def send_file_dingtalk(files, recipient_name_list, cf=None, employee_table=None):
    if cf is None or employee_table is None:
        if _is_hqyl_runtime():
            raise ValueError(
                "HQYL mode requires explicit cf and employee_table for DingTalk file delivery"
            )
        cfg, default_cf = _load_default_dingtalk_config()
        if cf is None:
            cf = default_cf
        if employee_table is None:
            user_map_path = os.path.join(config_path, "config", "user_map.yaml")
            with open(user_map_path, "r", encoding="utf-8") as config:
                employee_table = (yaml.safe_load(config) or {}).get("users") or {}
    required = ("app_key", "app_secret", "user_id", "space_id")
    missing = [name for name in required if not clean_text((cf or {}).get(name))]
    if missing:
        raise ValueError("DingTalk file config missing: " + ", ".join(missing))
    token, expire = get_access_token(cf['app_key'], cf['app_secret'])
    user_info = get_user_info(str(cf['user_id']), token)
    unionId = user_info['result']['unionid']
    space_id = str(cf['space_id'])
    parent_id = str(cf.get('parent_id') or '0')
    upload_candidates = _build_group_file_upload_candidates(
        default_space_id=space_id,
        default_parent_id=parent_id,
    )
    last_error = None
    result = None
    used_space_id = space_id
    for candidate_index, (target_space_id, target_parent_id) in enumerate(upload_candidates, start=1):
        try:
            result = upload_file_to_space(
                files,
                access_token=token,
                union_id=unionId,
                space_id=target_space_id,
                parent_id=target_parent_id,
                conflict_strategy="AUTO_RENAME",
            )
            used_space_id = target_space_id
            break
        except Exception as exc:
            last_error = exc
            if candidate_index >= len(upload_candidates):
                raise
            print(f"DingTalk file upload failed, trying fallback folder ({candidate_index}/{len(upload_candidates)}): {exc}")
    if result is None:
        raise last_error
    dentry_id = result.dentry.id
    print("提交结果:", dentry_id)

    userid_list = []
    for recipient_name in recipient_name_list:
        if recipient_name not in employee_table:
            print(f"收件人 '{recipient_name}' 不在 employee_table 中，跳过")
            continue
        userid_list.append(employee_table[recipient_name])

    unionId_list = []
    for userid in userid_list:
        user_info = get_user_info(userid, token)
        if 'result' not in user_info:
            print(f"获取用户 {userid} 信息失败: {user_info.get('errmsg', user_info)}")
            continue
        union_Id = user_info['result']['unionid']
        unionId_list.append(union_Id)

    if not unionId_list:
        print("没有有效的收件人，跳过文件发送")
        return

    for unid in unionId_list:
         send_ding_file(token, used_space_id, dentry_id, unid)


def send_text_msg(msg: str, access_token: str, secret: str = None, mobiles=None, is_at_all=False):
    """
    发送钉钉自定义机器人文本消息（支持加签安全模式）
    :param msg: 消息内容
    :param access_token: 机器人 access_token
    :param secret: 加签 secret，如果没有加签可传 None
    :param mobiles: 要@的手机号列表，不传或传空列表表示不@人
    :param is_at_all: 是否@所有人
    """
    if mobiles is None:
        mobiles = []

    # 处理加签
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        params = {"access_token": access_token, "timestamp": timestamp, "sign": sign}
    else:
        params = {"access_token": access_token}
    webhook = "https://oapi.dingtalk.com/robot/send"

    # 构造消息体
    data = {
        "msgtype": "text",
        "text": {"content": msg},
        "at": {
            "atMobiles": mobiles,
            "isAtAll": is_at_all
        }
    }

    headers = {"Content-Type": "application/json"}
    resp = requests.post(
        webhook,
        params=params,
        json=data,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errcode") not in (None, 0):
        raise RuntimeError(
            f"DingTalk robot send failed (code={payload.get('errcode')}): "
            f"{payload.get('errmsg') or 'unknown error'}"
        )
    return payload
