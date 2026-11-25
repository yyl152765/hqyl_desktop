import hashlib
import json
import os
import secrets
import time
import uuid

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/109.0.0.0 Safari/537.36'
)
BIGSELLER_ORIGIN = 'https://www.bigseller.pro'
DEFAULT_LOGIN_PAGE_URL = (
    'https://www.bigseller.pro/zh_CN/login.htm?'
    'redirect=https://www.bigseller.pro/web/login.htm'
)
DEFAULT_DASHBOARD_URL = f'{BIGSELLER_ORIGIN}/web/dashboard.htm'
DEFAULT_SHOPEE_ACTIVE_PAGE_URL = f'{BIGSELLER_ORIGIN}/web/listing/shopee/active/index.htm?bsStatus=4'
DEFAULT_SHOP_AUTH_PAGE_URL = f'{BIGSELLER_ORIGIN}/web/shop/index.htm?tab=allShops&go=shopee'
VERIFY_CODE_URL = f'{BIGSELLER_ORIGIN}/api/v2/genVerifyCode.json'
LOGIN_API_URL = f'{BIGSELLER_ORIGIN}/api/v3/auth/loginsub.json'
LOGIN_STATE_URL = f'{BIGSELLER_ORIGIN}/api/v1/isLogin.json'

CAPTCHA_TYPE_MAP = {
    '纯数字识别': 1,
    '纯数字识别-增强': 1001,
    '纯英文识别': 2,
    '纯英文识别-增强': 1002,
    '数英混合识别': 3,
    '数英混合识别-增强': 1003,
    '闪动GIF识别': 4,
    '计算题识别': 11,
    '快速计算题识别': 1005,
    '汉字识别': 16,
    '问答题识别': 66,
    'recaptcha图片识别': 49,
}


def _log(logger, level, message):
    if logger and hasattr(logger, level):
        getattr(logger, level)(message)


def get_request_user_agent(process_config):
    return (
        process_config.get('user_agent')
        or process_config.get('request', {}).get('header', {}).get('user-agent')
        or DEFAULT_USER_AGENT
    )


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def _normalize_proxies(proxy_config):
    if not proxy_config:
        return None
    if isinstance(proxy_config, str):
        proxy_url = proxy_config.strip()
        return {'http': proxy_url, 'https': proxy_url} if proxy_url else None
    if isinstance(proxy_config, dict):
        proxies = {
            key: value
            for key, value in proxy_config.items()
            if key in ('http', 'https') and value
        }
        return proxies or None
    return None


def build_request_session(process_config=None):
    request_config = (process_config or {}).get('request') or {}
    session = requests.Session()
    session.trust_env = _as_bool(request_config.get('use_env_proxy'), default=False)

    proxies = _normalize_proxies(request_config.get('proxies') or request_config.get('proxy'))
    if proxies:
        session.proxies.update(proxies)

    return session


def build_api_url(path):
    if path.startswith(('http://', 'https://')):
        return path
    normalized_path = path if path.startswith('/') else f'/{path}'
    if normalized_path.startswith('/api/'):
        return f'{BIGSELLER_ORIGIN}{normalized_path}'
    return f'{BIGSELLER_ORIGIN}/api{normalized_path}'


def get_listing_page_url(platform='shopee'):
    if platform == 'shopee':
        return DEFAULT_SHOPEE_ACTIVE_PAGE_URL
    return f'{BIGSELLER_ORIGIN}/web/listing/{platform}/index.htm'


def build_request_headers(user_agent, referer=DEFAULT_DASHBOARD_URL, content_type=None):
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Origin': BIGSELLER_ORIGIN,
        'Referer': referer,
        'User-Agent': user_agent,
    }
    if content_type:
        headers['Content-Type'] = content_type
    return headers


def get_tts_type_id(captcha_config):
    explicit_typeid = str(captcha_config.get('typeid', '')).strip()
    if explicit_typeid:
        return int(explicit_typeid)

    captcha_type_name = captcha_config.get('type_name', '数英混合识别-增强')
    if captcha_type_name not in CAPTCHA_TYPE_MAP:
        raise ValueError(f'不支持的打码类型: {captcha_type_name}')
    return CAPTCHA_TYPE_MAP[captcha_type_name]


def build_bs_metrics(user_agent, language='zh-CN', timezone='Asia/Taipei', screen='1920x1080'):
    metrics = {
        'userAgent': user_agent,
        'language': language,
        'screen': screen,
        'timezone': timezone,
        'hardwareConcurrency': os.cpu_count() or 'unknown',
        'deviceMemory': 'unknown',
    }
    raw = json.dumps(metrics, ensure_ascii=False, separators=(',', ':'))
    return 'fp-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def encrypt_bigseller_password(password):
    key = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)
    prefix = secrets.token_bytes(2)

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(password.encode('utf-8')) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return '0' + prefix.hex() + iv.hex() + key.hex() + ciphertext.hex()


def solve_captcha_by_tts(image_b64, captcha_config):
    provider = str(captcha_config.get('provider', 'ttshitu')).lower()
    if provider != 'ttshitu':
        raise ValueError(f'暂不支持的打码平台: {provider}')

    payload = {
        'username': captcha_config['username'],
        'password': captcha_config['password'],
        'typeid': get_tts_type_id(captcha_config),
        'image': image_b64,
    }
    url = captcha_config.get('url', 'http://api.ttshitu.com/predict')

    session = requests.Session()
    session.trust_env = _as_bool(captcha_config.get('use_env_proxy'), default=False)
    proxies = _normalize_proxies(captcha_config.get('proxies') or captcha_config.get('proxy'))
    if proxies:
        session.proxies.update(proxies)

    response = session.post(url, json=payload, timeout=30)
    response.raise_for_status()
    result = response.json()

    if result.get('success'):
        return result['data']['result']

    raise RuntimeError(result.get('message') or str(result))


def build_login_headers(user_agent, referer=DEFAULT_LOGIN_PAGE_URL):
    return build_request_headers(user_agent, referer=referer, content_type='application/json')


def fetch_verify_code(session, user_agent, login_page_url=DEFAULT_LOGIN_PAGE_URL):
    response = session.get(
        VERIFY_CODE_URL,
        timeout=20,
        headers=build_request_headers(user_agent, referer=login_page_url),
    )
    response.raise_for_status()
    data = response.json()
    if data.get('code') != 0:
        raise RuntimeError(f'获取验证码失败: {data}')
    return data['data']


def build_login_payload(account_config, verify_code, access_code, user_agent):
    auth_type = 'email' if '@' in account_config['username'] else 'phone'
    payload = {
        'account': account_config['username'],
        'password': encrypt_bigseller_password(account_config['password']),
        'picVerificationCode': verify_code,
        'accessCode': str(access_code),
        'fingerPrint': str(uuid.uuid4()),
        'authType': auth_type,
        'bsMetrics': build_bs_metrics(user_agent),
    }
    if auth_type == 'phone':
        payload['phoneAccountCode'] = str(account_config.get('phone_account_code', '86'))
    return payload


def login_bigseller_by_request(process_config, logger=None):
    account_config = process_config['bigseller_account']
    captcha_config = process_config['captcha_service']
    user_agent = get_request_user_agent(process_config)
    max_attempts = int(captcha_config.get('max_attempts', 8))
    last_error = '未开始登录'

    for attempt in range(1, max_attempts + 1):
        session = build_request_session(process_config)
        session.headers.update({'User-Agent': user_agent})

        try:
            session.get(DEFAULT_LOGIN_PAGE_URL, timeout=20)
            verify_data = fetch_verify_code(session, user_agent)
            image_b64 = verify_data['base64Image'].split(',', 1)[1]
            verify_code = solve_captcha_by_tts(image_b64, captcha_config).strip()

            login_response = session.post(
                LOGIN_API_URL,
                json=build_login_payload(
                    account_config,
                    verify_code,
                    verify_data['accessCode'],
                    user_agent,
                ),
                timeout=30,
                headers=build_login_headers(user_agent),
            )
            login_response.raise_for_status()
            login_data = login_response.json()

            if login_data.get('code') != 0:
                last_error = login_data.get('msg', str(login_data))
                _log(logger, 'warning', f'BigSeller 第{attempt}次登录失败，验证码[{verify_code}]，原因: {last_error}')
                time.sleep(1)
                continue

            state_response = session.get(
                LOGIN_STATE_URL,
                timeout=20,
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': f'{BIGSELLER_ORIGIN}/web/login.htm',
                    'User-Agent': user_agent,
                    'clientType': '1',
                },
            )
            state_response.raise_for_status()
            state_data = state_response.json()

            if state_data.get('code') == 0 and state_data.get('data') is True:
                _log(logger, 'info', f'BigSeller 第{attempt}次登录成功，验证码[{verify_code}]')
                return session

            last_error = f'登录态校验失败: {state_data}'
            _log(logger, 'warning', last_error)
        except Exception as exc:
            last_error = str(exc)
            _log(logger, 'warning', f'BigSeller 第{attempt}次登录异常: {last_error}')
            time.sleep(1)

    raise RuntimeError(f'BigSeller 请求登录失败，最后错误: {last_error}')


def get_session_cookie_dict(session):
    return requests.utils.dict_from_cookiejar(session.cookies)


def get_shop_authorization_page_url(platform='shopee', tab='allShops'):
    normalized_platform = str(platform or 'shopee').strip() or 'shopee'
    normalized_tab = str(tab or 'allShops').strip() or 'allShops'
    return f'{BIGSELLER_ORIGIN}/web/shop/index.htm?tab={normalized_tab}&go={normalized_platform}'


def get_shop_authorization_list_all(session, platform='shopee', tab='allShops', process_config=None):
    user_agent = get_request_user_agent(process_config or {})
    response = session.get(
        build_api_url('/v1/shop/authShop/list/all.json'),
        headers=build_request_headers(
            user_agent,
            referer=get_shop_authorization_page_url(platform=platform, tab=tab),
        ),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get('code') != 0:
        raise RuntimeError(f'获取 BigSeller 店铺授权列表失败: {data}')
    return data.get('data') or {}


def flatten_shop_authorization_rows(raw_data, platform=None):
    normalized_platform = str(platform or '').strip()
    if isinstance(raw_data, list):
        rows = raw_data
    elif isinstance(raw_data, dict):
        if normalized_platform:
            rows = raw_data.get(normalized_platform) or []
        else:
            rows = []
            for value in raw_data.values():
                if isinstance(value, list):
                    rows.extend(value)
    else:
        return []

    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_platform = str(row.get('platform') or '').strip()
        if normalized_platform and row_platform and row_platform != normalized_platform:
            continue
        normalized_rows.append(row)
    return normalized_rows


def get_shop_authorization_rows(session, platform='shopee', tab='allShops', process_config=None):
    raw_data = get_shop_authorization_list_all(
        session,
        platform=platform,
        tab=tab,
        process_config=process_config,
    )
    return flatten_shop_authorization_rows(raw_data, platform=platform)


def get_listing_sync_key(session, platform='shopee', process_config=None):
    user_agent = get_request_user_agent(process_config or {})
    response = session.get(
        build_api_url(f'/v1/process/getListingSyncKey/{platform}.json'),
        headers=build_request_headers(user_agent, referer=get_listing_page_url(platform)),
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get('code') != 0:
        raise RuntimeError(f'获取同步任务 key 失败: {data}')
    return data.get('data')


def trigger_listing_sync(session, platform='shopee', shop_id=None, shop_ids=None, process_config=None):
    user_agent = get_request_user_agent(process_config or {})
    params = {}
    if isinstance(shop_ids, (list, tuple, set)):
        params['shopIds'] = ','.join(str(item) for item in shop_ids if str(item).strip())
    elif shop_ids not in (None, ''):
        params['shopIds'] = str(shop_ids)
    elif shop_id not in (None, ''):
        params['shopId'] = str(shop_id)

    response = session.get(
        build_api_url(f'/v1/product/listing/{platform}/sync.json'),
        params=params,
        headers=build_request_headers(user_agent, referer=get_listing_page_url(platform)),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get('code') != 0:
        raise RuntimeError(f'触发同步失败: {data}')
    return data.get('data')


def check_process(session, sync_key, platform='shopee', model=None, process_config=None):
    user_agent = get_request_user_agent(process_config or {})
    params = {'key': sync_key}
    if model not in (None, ''):
        params['model'] = model

    response = session.get(
        build_api_url('/v1/process/check.json'),
        params=params,
        headers=build_request_headers(user_agent, referer=get_listing_page_url(platform)),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _get_process_info_map(result):
    data = result.get('data') if isinstance(result, dict) else None
    if isinstance(data, dict):
        info_map = data.get('infoMap')
        if isinstance(info_map, dict):
            return info_map
    info_map = result.get('infoMap') if isinstance(result, dict) else None
    return info_map if isinstance(info_map, dict) else {}


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _completion_value(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None

    normalized = str(value).strip().lower()
    if normalized in ('true', '1', 'yes', 'done', 'finish', 'finished', 'complete', 'completed', 'success'):
        return True
    if normalized in ('false', '0', 'no', 'running', 'processing', 'pending', 'syncing', 'doing'):
        return False
    return None


def _get_explicit_completion(result):
    containers = []
    if isinstance(result, dict):
        containers.append(result)
        data = result.get('data')
        if isinstance(data, dict):
            containers.append(data)

    completion_keys = ('complete', 'operaComplete', 'finished', 'finish', 'done', 'success')
    status_keys = ('status', 'state', 'processStatus', 'processState')
    for container in containers:
        for key in completion_keys:
            if key in container:
                parsed = _completion_value(container.get(key))
                if parsed is not None:
                    return parsed
        for key in status_keys:
            if key in container:
                parsed = _completion_value(container.get(key))
                if parsed is not None:
                    return parsed
    return None


def _has_process_counts(result):
    for item in _get_process_info_map(result).values():
        if not isinstance(item, dict):
            continue
        for key in ('successNum', 'failNum', 'allNum', 'handleNum', 'num'):
            if _safe_int(item.get(key) or 0) > 0:
                return True
    return False


def _process_info_signature(result):
    try:
        return json.dumps(_get_process_info_map(result), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(_get_process_info_map(result))


def _is_process_finished(result):
    if not isinstance(result, dict) or result.get('code') != 0:
        return False

    explicit_completion = _get_explicit_completion(result)
    if explicit_completion is not None:
        return explicit_completion

    info_map = _get_process_info_map(result)
    if not info_map:
        return False

    has_completion_flag = False
    missing_completion_flag = False
    for item in info_map.values():
        if not isinstance(item, dict):
            continue

        item_completion = None
        item_has_completion_signal = False
        for key in ('complete', 'operaComplete', 'finished', 'finish', 'done', 'success'):
            if key in item:
                parsed = _completion_value(item.get(key))
                if parsed is not None:
                    item_completion = parsed
                    item_has_completion_signal = True
                    has_completion_flag = True
                    break

        if item_completion is False:
            return False

        for key in ('status', 'state', 'processStatus', 'processState'):
            if key in item:
                parsed = _completion_value(item.get(key))
                if parsed is False:
                    return False
                if parsed is True:
                    item_has_completion_signal = True
                    has_completion_flag = True
        if not item_has_completion_signal:
            missing_completion_flag = True

    return has_completion_flag and not missing_completion_flag


def _format_process_progress(result):
    data = result.get('data') if isinstance(result, dict) else {}
    inner_code = data.get('code') if isinstance(data, dict) else None
    info_map = _get_process_info_map(result)
    if not info_map:
        return f'code={result.get("code")}, data.code={inner_code}, msg={result.get("msg", "")}'

    total = 0
    finished = 0
    handled = 0
    for item in info_map.values():
        if not isinstance(item, dict):
            continue
        total += 1
        handled += int(item.get('handleNum') or item.get('num') or 0)
        if item.get('complete') is True or item.get('operaComplete') is True:
            finished += 1

    return (
        f'code={result.get("code")}, data.code={inner_code}, '
        f'finished={finished}/{total}, handled={handled}, msg={result.get("msg", "")}'
    )


def wait_listing_sync_result(
    session,
    sync_key,
    platform='shopee',
    model=None,
    process_config=None,
    poll_interval_seconds=2,
    timeout_seconds=1800,
    logger=None,
):
    if sync_key in (None, '', -1, '-1'):
        raise ValueError('无效的同步任务 key')

    end_time = time.time() + int(timeout_seconds)
    last_result = None
    poll_count = 0
    stable_signature = None
    stable_poll_count = 0
    stable_result_polls = int((process_config or {}).get('sync', {}).get('stable_result_polls') or 3)
    while time.time() < end_time:
        try:
            result = check_process(
                session,
                sync_key,
                platform=platform,
                model=model,
                process_config=process_config,
            )
        except requests.exceptions.RequestException as exc:
            if last_result and _is_process_finished(last_result):
                _log(logger, 'warning', f'BigSeller 同步状态轮询超时，但上次结果已完成，key[{sync_key}]，异常: {exc}')
                return last_result

            poll_count += 1
            if poll_count == 1 or poll_count % 5 == 0:
                _log(logger, 'warning', f'BigSeller 同步状态轮询异常，继续等待，key[{sync_key}]，异常: {exc}')
            time.sleep(poll_interval_seconds)
            continue

        last_result = result
        if _is_process_finished(result):
            _log(logger, 'info', f'BigSeller 同步完成，key[{sync_key}]')
            return result

        if _has_process_counts(result):
            current_signature = _process_info_signature(result)
            if current_signature == stable_signature:
                stable_poll_count += 1
            else:
                stable_signature = current_signature
                stable_poll_count = 1

            if stable_poll_count >= stable_result_polls:
                _log(
                    logger,
                    'info',
                    f'BigSeller 同步结果连续 {stable_poll_count} 次未变化，按稳定结果返回，key[{sync_key}]',
                )
                return result
        else:
            stable_signature = None
            stable_poll_count = 0

        poll_count += 1
        if poll_count == 1 or poll_count % 5 == 0:
            _log(
                logger,
                'info',
                f'BigSeller 同步处理中，key[{sync_key}]，{_format_process_progress(result)}',
            )
        time.sleep(poll_interval_seconds)

    raise TimeoutError(f'BigSeller 同步等待超时，最后结果: {last_result}')


def inject_session_to_driver(driver, session, target_url=DEFAULT_DASHBOARD_URL, logger=None):
    driver.get(BIGSELLER_ORIGIN)
    driver.delete_all_cookies()
    added_count = 0

    for cookie in session.cookies:
        if cookie.domain and 'bigseller.pro' not in cookie.domain:
            continue

        cookie_data = {
            'name': cookie.name,
            'value': cookie.value,
            'path': cookie.path or '/',
        }
        if cookie.secure:
            cookie_data['secure'] = True
        if cookie.expires:
            cookie_data['expiry'] = int(cookie.expires)

        try:
            driver.add_cookie(cookie_data)
            added_count += 1
        except Exception:
            cookie_data.pop('expiry', None)
            try:
                driver.add_cookie(cookie_data)
                added_count += 1
            except Exception as exc:
                _log(logger, 'warning', f'写入浏览器 cookie 失败，已跳过 {cookie.name}: {exc}')

    driver.get(target_url)
    return added_count
