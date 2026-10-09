"""Offline execution of the real VN payment-wait functions, extracted with AST.

No business-module import, network, browser launch, payment, or database access.
"""

import ast
import hashlib
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[2] / "superbrowser_process/implement/shopee/vn_shopee_ads_recharge.py"
FUNCTIONS = {
    "_find_visible_elements", "_click_payment_ok_if_present",
    "_wait_payment_popup_closed", "_confirm_payment_success",
    "_get_payment_popup_text", "_get_visible_body_text", "_safe_current_url",
    "_TemporaryRemoteTimeout", "_text_has_payment_success",
    "_text_has_payment_fail", "_wait_for_payment_result",
}
CONSTANTS = {"STATUS_XPATH", "PAYMENT_SUCCESS_XPATH", "PAYMENT_OK_BUTTON_XPATH"}


class WebDriverException(Exception):
    pass


class InvalidSessionIdException(WebDriverException):
    pass


class NoSuchWindowException(WebDriverException):
    pass


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RemoteConnection:
    timeout = 60

    @classmethod
    def get_timeout(cls):
        return cls.timeout

    @classmethod
    def set_timeout(cls, value):
        cls.timeout = value


class Element:
    def __init__(self, driver, text="", is_success=False):
        self.driver, self.value, self.is_success = driver, text, is_success

    @property
    def text(self):
        if self.is_success:
            self.driver.success_observed = True
        return self.value

    def is_displayed(self):
        return True


class Driver:
    def __init__(self, namespace, error=None, error_after_success=False):
        self.ns = namespace
        self.error = error
        self.error_after_success = error_after_success
        self.success_observed = False
        self.window_reads = 0
        self.ok_clicked = False
        self.switch_to = SimpleNamespace(window=lambda handle: None)

    @property
    def window_handles(self):
        self.window_reads += 1
        if self.error and not self.error_after_success:
            raise self.error
        return ["fake-payment-result"]

    @property
    def current_url(self):
        return "https://offline.invalid/ads/purchase/result"

    @property
    def timeouts(self):
        if self.error_after_success and self.success_observed:
            raise self.error
        return SimpleNamespace(implicit_wait=0)

    def implicitly_wait(self, value):
        pass

    def find_elements(self, by, selector):
        if self.ok_clicked:
            return []
        if selector == self.ns["PAYMENT_SUCCESS_XPATH"]:
            return [Element(self, "Payment Successful!", is_success=True)]
        if selector == self.ns["PAYMENT_OK_BUTTON_XPATH"]:
            return [Element(self, "OK")]
        return []

    def execute_script(self, script, *args):
        if "innerText" in script:
            return "" if self.ok_clicked else "Payment Successful!"
        if "arguments[0].click()" in script:
            self.ok_clicked = True
        return None


def load_real_functions():
    source = SOURCE.read_text(encoding="utf-8-sig")
    parsed = ast.parse(source, filename=str(SOURCE))
    nodes = []
    for node in parsed.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets
        ):
            nodes.append(node)
    assert {node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.ClassDef))} == FUNCTIONS
    logs = []
    ns = {
        "re": re, "threading": threading, "RemoteConnection": RemoteConnection,
        "By": SimpleNamespace(XPATH="xpath", TAG_NAME="tag"),
        "WebDriverException": WebDriverException,
        "InvalidSessionIdException": InvalidSessionIdException,
        "NoSuchWindowException": NoSuchWindowException,
        "_REMOTE_TIMEOUT_LOCK": threading.Lock(),
        "_REMOTE_TIMEOUT_RESTORE_VALUE": None, "_REMOTE_TIMEOUT_USE_COUNT": 0,
        "log": SimpleNamespace(warning=logs.append),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns, logs, hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def run():
    ns, logs, source_hash = load_real_functions()
    cases = []
    definitions = [
        ("payment_success", None, False, True, 0),
        ("invalid_session", InvalidSessionIdException("invalid session id"), False, "processing", 0),
        ("no_such_window", NoSuchWindowException("no such window"), False, "processing", 0),
        ("disconnected", WebDriverException("disconnected: not connected to DevTools"), False, "processing", 0),
        ("connection_reset_only", WebDriverException("net::ERR_CONNECTION_RESET"), False, False, 6),
        ("success_then_cleanup_session_loss", InvalidSessionIdException("invalid session id during confirmation"), True, "processing", 0),
    ]
    for name, error, after_success, expected, expected_seconds in definitions:
        ns["time"] = clock = Clock()
        logs.clear()
        driver = Driver(ns, error=error, error_after_success=after_success)
        status, message = ns["_wait_for_payment_result"](driver, timeout=6)
        assert status == expected, (name, status, expected)
        assert clock.now == expected_seconds, (name, clock.now, expected_seconds)
        if after_success:
            assert driver.success_observed
        if name == "connection_reset_only":
            assert driver.window_reads == 6
        cases.append({
            "name": name, "passed": True, "returned_status": status,
            "message": message, "fake_seconds": clock.now,
            "window_reads": driver.window_reads,
            "success_observed": driver.success_observed,
            "ok_clicked": driver.ok_clicked, "warnings": list(logs),
        })
    return {
        "test": "vn_ads_payment_wait_offline_reproduction",
        "source": str(SOURCE), "source_sha256": source_hash,
        "mode": "AST-extracted original functions; fake clock, driver, and remote timeout transport",
        "passed": all(case["passed"] for case in cases),
        "cases": cases,
        "limitations": "No real Electron modal or store opening reproduced; ERR_CONNECTION_RESET is injected into WebDriver window enumeration only.",
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
