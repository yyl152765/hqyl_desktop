"""Public locations used by the desktop updater and release packager."""

from urllib.parse import urljoin


# Older clients migrate the bare IP to the retired DNS route. An explicit
# default port lets those clients save the same working URL as newer releases.
DEFAULT_UPDATE_MANIFEST_URL = "http://47.112.20.68:80/hqyl/latest.json"
DOMAIN_UPDATE_MANIFEST_URL = "http://rpa.whhqyl.com.cn/hqyl/latest.json"
ORIGIN_MANIFEST_URLS = (
    DEFAULT_UPDATE_MANIFEST_URL,
    "http://47.112.20.68/hqyl/latest.json",
)
OFFICIAL_MANIFEST_URLS = (*ORIGIN_MANIFEST_URLS, DOMAIN_UPDATE_MANIFEST_URL)


def official_installer_url(manifest_url: str, version: str) -> str:
    return urljoin(manifest_url, f"HQYLAutomationSetup_{version}.exe")
