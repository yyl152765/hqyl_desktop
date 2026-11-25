# FIELD_MAPPING = {
#     'browserIp': 'browser_ip',
#     'browserName': 'browser_name',
#     'browserOauth': 'browser_oauth',
#     'isDynamic': 'is_dynamic',
#     'isExpired': 'is_expired',
#     'proxyType': 'proxy_type',
#     'siteId': 'site_id',
#     'platform_id': 'platform_id',
#     'platform_name': 'platform_name',
#     'store_username': 'store_username',
#     'tags': 'tags'
# }
#
# PROJECT_PATH = r"D:\Python\ziniao_process"
# OUTPUT_PATH = r"\\1.1.1.28\rpa流程"
# ziniao_download ="D:\SuperBrowserDownloads\Super Browser"

class GlobalVar:

    env_args = []

    # 窗口大小
    window_height = 1200
    window_width = 1600

    screenshot_fail = 0
    # 页面加载超时时间
    page_load_timeout = 180


def get_env_args():
    return str(GlobalVar.env_args)


def get_env_args_list():
    return GlobalVar.env_args


def get_screenshot_fail():
    return GlobalVar.screenshot_fail


def get_page_load_timeout():
    return GlobalVar.page_load_timeout
