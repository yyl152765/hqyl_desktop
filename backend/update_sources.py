"""The shared desktop update endpoint and the private store behind it."""
from urllib.parse import quote, unquote, urlsplit

DEFAULT_UPDATE_MANIFEST_URL = "https://hrms.whhqyl.com.cn/desktop-updates/latest.json"
DOMAIN_UPDATE_MANIFEST_URL = "http://rpa.whhqyl.com.cn/hqyl/latest.json"
LEGACY_MANIFEST_URLS = (
    DOMAIN_UPDATE_MANIFEST_URL,
    "http://47.112.20.68:80/hqyl/latest.json",
    "http://47.112.20.68/hqyl/latest.json",
)
OSS_HOST = "hq-middleground.oss-cn-hangzhou.aliyuncs.com"
OSS_DIRECTORY = "/自动化平台/"


def normalize_manifest_url(value: str) -> str:
    url = str(value or "").strip()
    if not url or url in LEGACY_MANIFEST_URLS:
        return DEFAULT_UPDATE_MANIFEST_URL
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname == "hrms.whhqyl.com.cn"
                and parts.port in (None, 443) and parts.path == "/desktop-updates/latest.json"
                and not parts.query and not parts.fragment):
            return DEFAULT_UPDATE_MANIFEST_URL
    except ValueError:
        pass
    return url


def installer_object_url(version: str) -> str:
    return f"https://{OSS_HOST}{quote(OSS_DIRECTORY)}HQYLAutomationSetup_{version}.exe"


def official_oss_package(url: str, version: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == OSS_HOST and parts.port in (None, 443)
                and parts.username is None and parts.password is None
                and unquote(parts.path) == f"{OSS_DIRECTORY}HQYLAutomationSetup_{version}.exe")
    except ValueError:
        return False
