import os
import urllib.request


PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_true(name):
    return str(os.getenv(name) or "").strip().lower() in TRUE_VALUES


def apply_default_network_policy():
    """Keep RPA HTTP calls independent from local desktop proxy state.

    Most scheduled flows should connect directly. A user-level proxy such as
    127.0.0.1:7892 makes requests fail when the proxy app is not running.
    Set RPA_ALLOW_ENV_PROXY=1 for a process that intentionally needs it.
    """
    if _env_true("RPA_ALLOW_ENV_PROXY"):
        return False

    for name in PROXY_ENV_VARS:
        os.environ.pop(name, None)

    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

    try:
        urllib.request.install_opener(
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
        )
    except Exception:
        pass

    return True
