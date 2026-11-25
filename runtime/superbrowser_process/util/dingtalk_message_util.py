# -*- coding: utf-8 -*-
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys, os
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

#获取token
def get_access_token(app_key: str, app_secret: str, *, session=None, timeout=None):
    if session is not None:
        # Explicit transport mode lets portable callers bound requests without
        # loading configuration or permitting credential-bearing redirects.
        response = session.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": app_key, "appSecret": app_secret},
            timeout=timeout or (10, 30), allow_redirects=False,
        )
        try:
            if response.status_code != 200:
                raise ValueError("dingtalk_access_token_http_rejected")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("dingtalk_access_token_response_invalid")
            token, ttl = value.get("accessToken"), value.get("expireIn")
            if (not isinstance(token, str) or not token or len(token) > 8192
                    or any(char.isspace() for char in token) or type(ttl) is not int or ttl <= 60):
                raise ValueError("dingtalk_access_token_response_invalid")
            return token, ttl
        finally:
            response.close()
    # 初始化 client
    config = open_api_models.Config(protocol='https', region_id='central')
    client = DingtalkClient(config)

    # 请求 access_token
    req = dingtalk_models.GetAccessTokenRequest(app_key=app_key, app_secret=app_secret)
    resp = client.get_access_token(req)

    return resp.body.access_token, resp.body.expire_in
#获取用户信息
def get_user_info(user_id, access_token):
    url = "https://oapi.dingtalk.com/topapi/v2/user/get"
    data = {
        "userid": user_id,
        "language": "zh_CN"
    }
    resp = requests.post(url, params={"access_token": access_token}, data=data).json()
    return resp


def load_dingtalk_config_and_user_map():
    with open(rf'{config_path}/config/config.yaml', 'r', encoding='utf-8') as config:
        cfg = yaml.safe_load(config)
    with open(rf'{config_path}/config/user_map.yaml', 'r', encoding='utf-8') as config:
        employee_table = yaml.safe_load(config)['users']
    return cfg, employee_table


def resolve_user_ids(recipient_name_list, employee_table: dict):
    missing_names = [recipient_name for recipient_name in recipient_name_list if recipient_name not in employee_table]
    if missing_names:
        raise KeyError(f"未在 user_map.yaml 中找到接收人: {', '.join(missing_names)}")
    return [str(employee_table[recipient_name]) for recipient_name in recipient_name_list]

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
def get_upload_info(client, access_token, space_id, union_id, parent_id, file_path):
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

    resp = client.get_file_upload_info_with_options(
        space_id, request, headers, util_models.RuntimeOptions()
    )
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
        if response.status_code == 200:
            print("上传成功")
        else:
            print(f"上传失败，状态码：{response.status_code}")
            print(response.text)

    except Exception as e:
        print(f"上传异常: {e}")
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
            util_models.RuntimeOptions()
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
        client.send_by_app_with_options(request, headers, util_models.RuntimeOptions())
        print(f"文件 {dentry_id} 已发送给用户 {union_id}")
    except Exception as err:
            print(f"发送失败: {err}")


def send_file_dingtalk(files, recipient_name_list,cf =None):
    cfg, employee_table = load_dingtalk_config_and_user_map()
    if cf is None:
        cf = cfg['dingtalk']['app']['rpa']
    file_size = os.path.getsize(files)
    file_name = os.path.basename(files)
    token, expire = get_access_token(cf['app_key'], cf['app_secret'])
    user_info = get_user_info(str(cf['user_id']), token)
    unionId = user_info['result']['unionid']
    # 初始化 client
    client = create_client()
    # 获取上传信息
    info = get_upload_info(client, token, str(cf['space_id']), unionId, str(cf['parent_id']), files)
    # 提取信息
    resource_url = info['body']["headerSignatureInfo"]["resourceUrls"][0]
    headers = info['body']["headerSignatureInfo"]["headers"]
    upload_key = info['body']["uploadKey"]
    upload_file_with_header_signature(resource_url, headers, files)
    result = commit_file(client, token, str(cf['space_id']), unionId, upload_key, file_name, str(cf['parent_id']), file_size,
                         conflict_strategy="AUTO_RENAME", app_properties=None)
    dentry_id = result.dentry.id
    print("提交结果:", dentry_id)

    userid_list = resolve_user_ids(recipient_name_list, employee_table)
    unionId_list = []
    for userid in userid_list:
        user_info = get_user_info(userid, token)
        union_Id = user_info['result']['unionid']
        unionId_list.append(union_Id)
    for unid in unionId_list:
         send_ding_file(token, str(cf['space_id']), dentry_id, unid)


def send_app_text_msg(msg: str, recipient_name_list, cf=None, *, user_map=None,
                      timeout=None, session=None, token_provider=None):
    # Legacy calls retain their YAML defaults. Supplying both explicit inputs
    # avoids opening either legacy file, including in a packaged executable.
    if cf is None or user_map is None:
        cfg, employee_table = load_dingtalk_config_and_user_map()
        if cf is None:
            cf = cfg['dingtalk']['app']['rpa']
        if user_map is None:
            user_map = employee_table

    userid_list = resolve_user_ids(recipient_name_list, user_map)
    if token_provider is not None:
        token, _ = token_provider(cf['app_key'], cf['app_secret'])
    elif session is not None:
        token, _ = get_access_token(cf['app_key'], cf['app_secret'], session=session, timeout=timeout)
    else:
        token, _ = get_access_token(cf['app_key'], cf['app_secret'])
    url = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"
    payload = {
        "agent_id": int(cf["agent_id"]),
        "userid_list": ",".join(userid_list),
        "msg": {
            "msgtype": "text",
            "text": {
                "content": msg,
            },
        },
    }
    if session is not None:
        payload["to_all_user"] = False
    headers = {"Content-Type": "application/json"}
    options = {}
    if timeout is not None:
        options["timeout"] = timeout
    if session is not None:
        options["allow_redirects"] = False
    response = (session or requests).post(url, params={"access_token": token}, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, **options)
    if session is None:
        return response.json()
    try:
        if response.status_code != 200:
            if type(response.status_code) is int and 400 <= response.status_code < 500 and response.status_code != 408:
                raise ValueError("dingtalk_message_http_rejected")
            raise ValueError("dingtalk_message_http_unconfirmed")
        return response.json()
    finally:
        response.close()


def _app_request_options(session, timeout):
    if session is None or not callable(getattr(session, "post", None)):
        raise ValueError("dingtalk_app_config_invalid")
    if (not isinstance(timeout, tuple) or len(timeout) != 2
            or any(type(value) not in (int, float) or not 0 < value <= 300 for value in timeout)):
        raise ValueError("dingtalk_app_config_invalid")


def _app_token(cf, token_provider, *, agent_required=False):
    if not isinstance(cf, Mapping) or not callable(token_provider):
        raise ValueError("dingtalk_app_config_invalid")
    key, secret = cf.get("app_key"), cf.get("app_secret")
    if any(not isinstance(value, str) or not 0 < len(value) <= 1024
           or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
           for value in (key, secret)):
        raise ValueError("dingtalk_app_config_invalid")
    agent_id = cf.get("agent_id")
    if agent_required:
        if isinstance(agent_id, str):
            if not 0 < len(agent_id) <= 19 or not agent_id.isascii() or not agent_id.isdecimal():
                raise ValueError("dingtalk_app_config_invalid")
            agent_id = int(agent_id)
        if type(agent_id) is not int or not 0 < agent_id < 2 ** 63:
            raise ValueError("dingtalk_app_config_invalid")
    try:
        token, ttl = token_provider(key, secret)
    except Exception:
        raise ValueError("dingtalk_app_token_failed") from None
    if (not isinstance(token, str) or not 0 < len(token) <= 8192
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in token)
            or type(ttl) is not int or ttl <= 0):
        raise ValueError("dingtalk_app_token_failed")
    return token, agent_id


def _app_post_json(url, token, *, session, timeout, params=None, **payload):
    """Single attempt; caller decides whether a failed send is uncertain."""
    try:
        response = session.post(
            url, params={"access_token": token, **(params or {})},
            timeout=timeout, allow_redirects=False, **payload,
        )
    except Exception:
        raise ValueError("dingtalk_app_request_unconfirmed") from None
    try:
        try:
            status = response.status_code
        except Exception:
            raise ValueError("dingtalk_app_request_unconfirmed") from None
        if type(status) is not int or status != 200:
            if type(status) is int and 400 <= status < 500 and status != 408:
                raise ValueError("dingtalk_app_http_rejected")
            raise ValueError("dingtalk_app_request_unconfirmed")
        try:
            value = response.json()
        except Exception:
            raise ValueError("dingtalk_app_request_unconfirmed") from None
        if not isinstance(value, dict):
            raise ValueError("dingtalk_app_request_unconfirmed")
        return value
    finally:
        try:
            response.close()
        except Exception:
            pass


def upload_app_file(file_path, cf, *, session, timeout=(10, 30), token_provider):
    """Upload an Excel attachment without loading configuration or sending it."""
    _app_request_options(session, timeout)
    try:
        path = Path(file_path)
        if path.suffix.lower() not in (".xls", ".xlsx"):
            raise ValueError()
        stream = path.open("rb")
    except (OSError, TypeError, ValueError):
        raise ValueError("dingtalk_app_file_invalid") from None
    with stream:
        try:
            size = os.fstat(stream.fileno()).st_size
        except OSError:
            raise ValueError("dingtalk_app_file_invalid") from None
        if not 0 < size <= 10 * 1024 * 1024:
            raise ValueError("dingtalk_app_file_invalid")
        token, _ = _app_token(cf, token_provider)
        content_type = ("application/vnd.ms-excel" if path.suffix.lower() == ".xls"
                        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        value = _app_post_json(
            "https://oapi.dingtalk.com/media/upload", token,
            session=session, timeout=timeout, params={"type": "file"},
            files={"media": (path.name, stream, content_type)},
        )
    code = value.get("errcode")
    if type(code) is int and code != 0:
        raise ValueError("dingtalk_app_upload_rejected")
    media_id = value.get("media_id")
    if (type(code) is not int or code != 0 or value.get("type") != "file"
            or not isinstance(media_id, str) or not 0 < len(media_id) <= 2048
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in media_id)):
        raise ValueError("dingtalk_app_upload_unconfirmed")
    return media_id


def send_app_file_msg(media_id, names, cf, *, user_map, session, timeout=(10, 30), token_provider):
    """Send uploaded media through work notifications, returning the API receipt."""
    _app_request_options(session, timeout)
    if (not isinstance(media_id, str) or not 0 < len(media_id) <= 2048
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in media_id)):
        raise ValueError("dingtalk_app_media_invalid")
    if (not isinstance(names, Sequence) or isinstance(names, (str, bytes))
            or not names or len(names) > 100 or not isinstance(user_map, Mapping)
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("dingtalk_app_recipient_invalid")
    user_ids = [user_map.get(name) for name in names]
    if (any(not isinstance(user_id, str) or not 0 < len(user_id) <= 256
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 or char == "," for char in user_id)
            for user_id in user_ids) or len(set(user_ids)) != len(user_ids)):
        raise ValueError("dingtalk_app_recipient_invalid")
    token, agent_id = _app_token(cf, token_provider, agent_required=True)
    return _app_post_json(
        "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2", token,
        session=session, timeout=timeout,
        json={"agent_id": agent_id, "userid_list": ",".join(user_ids), "to_all_user": False,
              "msg": {"msgtype": "file", "file": {"media_id": media_id}}},
    )


def _app_message_query(task_id, cf, *, session, timeout, token_provider, operation):
    _app_request_options(session, timeout)
    if type(task_id) is not int or not 0 < task_id < 2 ** 63:
        raise ValueError("dingtalk_app_task_invalid")
    token, agent_id = _app_token(cf, token_provider, agent_required=True)
    return _app_post_json(
        "https://oapi.dingtalk.com/topapi/message/corpconversation/" + operation, token,
        session=session, timeout=timeout, json={"agent_id": agent_id, "task_id": task_id},
    )


def get_app_message_send_result(task_id, cf, *, session, timeout=(10, 30), token_provider):
    """Query a known task only; no message is sent or retried."""
    return _app_message_query(task_id, cf, session=session, timeout=timeout,
                              token_provider=token_provider, operation="getsendresult")


def get_app_message_send_progress(task_id, cf, *, session, timeout=(10, 30), token_provider):
    """Query completion progress for a known work notification task."""
    return _app_message_query(task_id, cf, session=session, timeout=timeout,
                              token_provider=token_provider, operation="getsendprogress")


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
        webhook = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}"
    else:
        webhook = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}"

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
    resp = requests.post(webhook, json=data, headers=headers)
    return resp.json()
