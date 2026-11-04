"""TemuActivityGateway driven by a scripted fake page (no real browser)."""
from __future__ import annotations

import base64
import json
import itertools
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    sync_playwright = None

from backend.services import temu_activity_export as export
from backend.services import temu_activity_page as page
from tests.test_temu_activity_export import QUOTE_ROWS, build_template

STORE = {"store_id": "s1", "store_name": "甲-店A", "platform_name": "TEMU"}

# The counter regexes are taken from the real page script so the tests follow it.
TOTAL_PATTERNS = re.findall(r"scopeText\.match\(/(.+?)/\)", page.PAGE_STATE)


def js_counters(text: str, in_dialog: bool) -> dict:
    total_main, total_dialog, selected = TOTAL_PATTERNS
    num = lambda value: int(re.sub(r"[^\d]", "", value))  # noqa: E731
    total = re.search(total_main, text) or (re.search(total_dialog, text) if in_dialog else None)
    chosen = re.search(selected, text)
    return {"total": num(total.group(1)) if total else None, "selected": num(chosen.group(1)) if chosen else None}


def node(selector, text="", selected=False, checked=False, disabled=False):
    return {"selector": selector, "text": text, "selected": selected, "checked": checked, "disabled": disabled}


class FakeBrowser:
    profile = "p"
    auth_failed = False
    cleanup_failed = False

    def __init__(self, xlsx: bytes, *, pages=2, total=150, selected=150, page_text="", topics=None, filters=None):
        self.xlsx = xlsx
        self.pages, self.page = pages, 1
        self.total, self.selected = total, selected
        self.page_text = page_text
        self.on_activity = False
        self.types = {label: False for label in page.ACTIVITY_TYPES}
        self.topics = topics if topics is not None else {"国庆专题": False, "季末清仓专题": False, "双11主题": False}
        self.filters = filters or {}
        self.topics_all = False
        self.sites = {"菲律宾": False, "马来西亚": False, "泰国": False}
        self.modal = self.size100 = self.size_open = False
        self.page_checked: set[int] = set()
        self.closed: list[str] = []

    def ready(self):
        pass

    def list_stores(self):
        return [dict(STORE)]

    def open_store(self, store, url):
        self.opened = url

    def close_store(self, store_id):
        self.closed.append(store_id)

    def click(self, store_id, selector):
        kind, _, arg = selector.partition(":")
        if kind == "menu" and arg == "批量报名活动":
            self.on_activity = True
        elif kind == "type":
            self.types[arg] = not self.types[arg]
        elif kind == "topicall":
            self.topics_all = True
            self.topics = {name: True for name in self.topics}
        elif kind == "topic":
            self.topics[arg] = not self.topics[arg]
        elif kind == "filter":
            self.filters[arg] = not self.filters[arg]
        elif kind == "site":
            self.sites[arg] = not self.sites[arg]
        elif kind == "entry":
            self.modal = True
        elif kind == "size":
            self.size_open = True
        elif kind == "size100":
            self.size100, self.size_open = True, False
        elif kind == "prodall":
            self.page_checked.add(self.page)
        elif kind == "next":
            self.page += 1

    def evaluate(self, store_id, script):
        if "state.wrapper" in script:
            return {"ok": True}
        if "files.map" in script:
            return {"files": [{"size": len(self.xlsx)}], "pending": 0, "error": ""}
        if "btoa" in script:
            start, end = map(int, re.search(r"subarray\((\d+),(\d+)\)", script).groups())
            return base64.b64encode(self.xlsx[start:end]).decode()
        if "removeEventListener" in script:
            return True
        return self._state()

    def _state(self):
        found = {label: [] for label in page.LABELS}
        checkables, topics = [], None
        if not self.on_activity:
            for label in ("店铺营销", "营销活动", "批量报名活动"):
                found[label] = [node(f"menu:{label}", label)]
        else:
            for label in page.ACTIVITY_TYPES:
                found[label] = [node(f"type:{label}", label, selected=self.types[label])]
            topics = [node(f"topic:{name}", name, checked=on) for name, on in self.topics.items()]
            filters = [node(f"filter:{name}", name, checked=on) for name, on in self.filters.items()]
            checkables = topics + filters
            if not self.modal:
                found["全选"] = [node("topicall:", "全选", checked=self.topics_all)]
                found["选择商品"] = [node("entry:", "选择商品")]
            for name, on in self.sites.items():
                found[name] = [node(f"site:{name}", name, checked=on)]
            found["生成模板"] = [node("generate:", "生成模板")]
            if self.modal:
                found["全选"] = [node("prodall:", "全选", checked=self.page in self.page_checked)]
                if self.size100:
                    found["100条/页"] = [node("size100:", "100条/页", selected=True)]
                else:
                    found["20条/页"] = [node("size:", "20条/页")]
                    if self.size_open:
                        found["100条/页"] = [node("size100:", "100条/页")]
                if self.page < self.pages:
                    found["下一页"] = [node("next:", "下一页")]
        body = self.page_text + " 活动报名"
        dialog = f"共 {self.total} 个商品 已选 {self.selected} 第{self.page}页" if self.modal else ""
        scope = dialog if self.modal else body
        return {"url": "https://agentseller.temu.com/x", "host": "agentseller.temu.com", "title": "t",
                "login": False, "verification": False, "busy": False, "found": found,
                "checkables": checkables, "topics": topics,
                "counters": js_counters(scope, self.modal), "scope_sig": f"{len(scope)}:{hash(scope)}",
                "in_dialog": self.modal, "body": (body + dialog)[:20000]}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        sleeper = patch.object(page.time, "sleep", lambda *_: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def xlsx(self, site="菲律宾", *, rows=None) -> bytes:
        return build_template(self.root / "src" / f"{site}.xlsx", site=site,
                              rows=QUOTE_ROWS[:2] if rows is None else rows).read_bytes()

    def gateway(self, browser):
        gateway = page.TemuActivityGateway("p", browser=browser, timeout=2)
        gateway.preflight([{**STORE, "status": "pending"}])
        return gateway

    def test_full_export_flow(self):
        browser = FakeBrowser(self.xlsx())
        result = self.gateway(browser).export(STORE, self.raw, sites=["PH"])
        evidence = result["evidence"]
        self.assertEqual(Path(result["path"]).read_bytes(), browser.xlsx)
        self.assertEqual(evidence["activity_types"]["unverified"], [])
        self.assertEqual(evidence["clearance"]["excluded"], ["季末清仓专题"])
        self.assertEqual(browser.topics, {"国庆专题": True, "季末清仓专题": False, "双11主题": True})
        self.assertEqual(evidence["products"]["pages"], 2)
        self.assertTrue(evidence["products"]["page_size_100"])
        self.assertTrue(evidence["products"]["count_match"])
        self.assertEqual(browser.page_checked, {1, 2})
        self.assertEqual(browser.closed, ["s1"])

    def test_run_batch_with_real_gateway(self):
        path = export.create_batch(self.root / "out", "p", [STORE], ["PH"], "2026-11-03")
        result = export.run_batch(path, self.gateway(FakeBrowser(self.xlsx())))
        self.assertTrue(result["is_complete"], result["stores"][0]["error"])
        self.assertIn("菲律宾", Path(result["stores"][0]["result_file"]).name)

    def test_foreign_or_missing_site_fails_closed(self):
        browser = FakeBrowser(self.xlsx("泰国"))
        with self.assertRaisesRegex(page.ActivityPageError, "未勾选站点"):
            self.gateway(browser).export(STORE, self.raw, sites=["PH"])
        self.assertEqual(browser.closed, ["s1"])
        second = self.raw / "b"
        second.mkdir()
        with self.assertRaisesRegex(page.ActivityPageError, "缺少勾选站点"):
            self.gateway(FakeBrowser(self.xlsx())).export(STORE, second, sites=["PH", "TH"])

    def test_selected_count_mismatch_blocks_export(self):
        with self.assertRaisesRegex(page.ActivityPageError, "不一致"):
            self.gateway(FakeBrowser(self.xlsx(), selected=100)).export(STORE, self.raw, sites=["PH"])

    def test_clearance_in_download_is_rejected_even_when_page_exclusion_succeeds(self):
        browser = FakeBrowser(self.xlsx(rows=QUOTE_ROWS))
        with self.assertRaisesRegex(page.ActivityPageError, "清仓"):
            self.gateway(browser).export(STORE, self.raw, sites=["PH"])
        self.assertFalse(browser.topics["季末清仓专题"])
        self.assertEqual(browser.closed, ["s1"])

    def test_unrelated_total_text_is_not_the_product_total(self):
        self.assertIsNone(js_counters("专题活动 共3个活动可报名 共5页", False)["total"])
        self.assertEqual(js_counters("共 1,234 个商品 已选：1,234", False), {"total": 1234, "selected": 1234})
        self.assertEqual(js_counters("共 150 条 已选 150", True)["total"], 150)
        browser = FakeBrowser(self.xlsx(), page_text="共3个活动可报名 共5页 ")
        result = self.gateway(browser).export(STORE, self.raw, sites=["PH"])
        self.assertTrue(result["evidence"]["products"]["count_match"])

    def test_page_turn_detected_beyond_long_page_text(self):
        browser = FakeBrowser(self.xlsx(), page_text="菜单" * 20000)
        result = self.gateway(browser).export(STORE, self.raw, sites=["PH"])
        self.assertEqual(result["evidence"]["products"]["pages"], 2)

    def test_page_turn_without_change_still_fails(self):
        browser = FakeBrowser(self.xlsx())
        browser.click = (lambda original: lambda sid, sel: None if sel == "next:" else original(sid, sel))(browser.click)
        with patch.object(page.time, "monotonic", side_effect=itertools.count(0, 100)):
            gateway = self.gateway(browser)
            gateway.open_activity_page(STORE)
            with self.assertRaisesRegex(page.ActivityPageError, "翻页后页面内容未变化"):
                gateway.select_all_products("s1")

    def test_clearance_only_touches_topic_group(self):
        browser = FakeBrowser(self.xlsx(), filters={"仅看清仓商品": True})
        gateway = self.gateway(browser)
        gateway.open_activity_page(STORE)
        result = gateway.exclude_clearance("s1")
        self.assertEqual(result["excluded"], ["季末清仓专题"])
        self.assertTrue(browser.filters["仅看清仓商品"])

    def test_missing_topic_group_is_reported(self):
        browser = FakeBrowser(self.xlsx())
        gateway = self.gateway(browser)
        gateway.open_activity_page(STORE)
        browser._state = (lambda original: lambda: {**original(), "topics": None})(browser._state)
        with self.assertRaisesRegex(page.ActivityPageError, "专题主题列表"):
            gateway.exclude_clearance("s1")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_state_script_is_valid_javascript(self):
        script = self.root / "state.js"
        script.write_text(page.page_state_script(), encoding="utf-8")
        completed = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class PageStateDOMTests(unittest.TestCase):
    """Run the actual DOM script and site-selection code in an offline browser."""

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is required for DOM checks")
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.runtime.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        else:
            raise unittest.SkipTest("Chromium/Edge is required for DOM checks")
        cls.addClassCleanup(cls.browser.close)

    def test_select_sites_uses_real_page_state_for_every_site_alias(self):
        for code, aliases in page.SITE_DISPLAY.items():
            for alias in aliases:
                with self.subTest(site=code, label=alias):
                    context = self.browser.new_context()
                    try:
                        view = context.new_page()
                        def serve(route):
                            route.fulfill(content_type="text/html; charset=utf-8",
                                          body=f'<label id="site"><input type="checkbox">{alias}</label>')

                        context.route("**/*", serve)
                        view.goto("https://agentseller.temu.com/offline-test")

                        class DOMBrowser:
                            profile = "p"
                            auth_failed = False

                            def evaluate(self, store_id, script):
                                return json.loads(view.evaluate(script))

                            def click(self, store_id, selector):
                                view.locator(selector).click()

                        gateway = page.TemuActivityGateway("p", browser=DOMBrowser())
                        with patch.object(page.time, "sleep", lambda *_: None):
                            gateway.select_sites("s1", [code])
                            gateway.select_sites("s1", [code])
                        self.assertTrue(view.locator('input[type="checkbox"]').is_checked())
                    finally:
                        context.close()


class LeapDayTests(unittest.TestCase):
    def test_validate_request_on_february_29(self):
        class LeapDay(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2028, 2, 29, 12, tzinfo=tz)

        with patch.object(export, "datetime", LeapDay):
            options = export.validate_request({"store_names": "A", "sites": ["PH"], "business_date": "2027-02-28"})
            self.assertEqual(options["business_date"], "2027-02-28")
            with self.assertRaisesRegex(ValueError, "一年前"):
                export.validate_request({"store_names": "A", "sites": ["PH"], "business_date": "2027-02-27"})


if __name__ == "__main__":
    unittest.main()
