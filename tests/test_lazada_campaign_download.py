"""Exercise the campaign downloader against a local Seller Center flow, including popups."""
import base64
import io
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock, call, patch

from openpyxl import Workbook, load_workbook
from playwright.sync_api import expect, sync_playwright

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services import download_lazada_campaign as campaign
from backend.services.lazada_campaign_agreement import agreement_dialog, confirm_required_agreements

HOST = "sellercenter-my.lazada-seller.cn"
ORIGIN = "https://" + HOST
TITLE = "[Extra Exposure] BAU December 2026 (1 Dec - 6 Dec)"
DETAIL = "/portal/campaign/scene/detail/40953"
SCENE_HTML = """
<meta charset="utf-8"><button onclick="menu.hidden=!menu.hidden">用Excel提交</button>
<div id="menu" hidden>
  <button onclick="menu.hidden=true; historyPanel()">导入/导出历史</button>
  <button onclick="menu.hidden=true; options.hidden=false">下载Excel</button>
</div>
<div role="dialog" hidden id="options">
  <button onclick="options.hidden=true; sessionStorage.exports=+(sessionStorage.exports||0)+1; historyPanel()">下载</button>
</div>
<div role="dialog" hidden id="panel"><b>历史文件</b>
  <button aria-label="关闭" onclick="panel.hidden=true; sessionStorage.closes=+(sessionStorage.closes||0)+1">×</button>
  <b>导出记录</b><table><tbody id="records"></tbody></table>
</div>
<script>
function row(id, state) {
    return '<tr><td>'+id+'</td><td>template.xlsx</td><td>2026-11-20</td><td>'+state+
        '</td><td><a download="template.xlsx" onclick="sessionStorage.downloadId='+id+
        '" href="data:application/octet-stream;base64,WORKBOOK">下载</a></td></tr>';
}
function historyPanel() {
    records.innerHTML = row(11, '成功');
    if (sessionStorage.exports) {
        records.innerHTML = row(12, sessionStorage.ready ? '成功' : '生成中') + records.innerHTML;
    }
    panel.hidden=false;
}
addEventListener('pagehide', () => {
    if (panel.hidden && sessionStorage.exports) sessionStorage.ready='yes';
});
</script>
"""

AGREEMENT_HTML = """
<div id="terms" role="dialog" hidden><div class="next-dialog">
  <h2>大促协议</h2><h3>基础活动协议</h3>
  <section><header><label><input type="checkbox" id="product" checked>商品协议</label><b>强烈推荐</b></header>
    <table><tr><td><label><input id="deals" type="checkbox" checked>Deals</label></td>
      <td><input id="accelerator" type="checkbox" hidden checked><label for="accelerator">Agree</label></td></tr></table>
  </section>
  <h3>更多协议</h3>
  <section><header><input id="platform" type="checkbox"><span>平台优惠协议</span><b>必选</b></header>
    <label><input type="radio" name="scope" id="approved" checked>仅限已审核的大促商品</label>
    <label><input type="radio" name="scope" id="all">所有商品</label>
    <label><input type="checkbox" id="extra">额外推广</label>
  </section>
  <label><input type="checkbox" required checked disabled id="locked">基础条款</label>
  <button id="confirm">确定</button>
</div></div>
<script>
document.querySelector('#platform').onchange = () => { document.querySelector('#extra').checked = true; };
document.querySelector('#product').onchange = event => {
    if (!event.target.checked) document.querySelector('#terms table').hidden = true;
};
document.querySelector('#confirm').onclick = () => {
    sessionStorage.agreed = JSON.stringify([...document.querySelectorAll('#terms input:checked')].map(box => box.id));
    sessionStorage.confirmCount = Number(sessionStorage.confirmCount || 0) + 1;
    document.querySelector('#terms').hidden = true;
    window.afterAgreement?.();
};
</script>
"""


def tab_group(tabs, selected):
    controls = []
    panels = []
    for key, title, content in tabs:
        active = key == selected
        controls.append(f'<button role="tab" aria-selected="{str(active).lower()}" aria-controls="{key}" '
                        f'onclick="selectPanel(this)"><span>{title}</span></button>')
        panels.append(f'<section role="tabpanel" id="{key}" {"" if active else "hidden"}>{content}</section>')
    return '<div role="tablist">' + ''.join(controls) + '</div>' + ''.join(panels)


def fixture_workbook(text):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([text])
    sheet.append(["SKU", "原价", "活动价", "推荐价"])
    sheet.append(["A", 100, 95, 92.75])
    sheet.append(["B", 100, 94.99, 92.74])
    sheet.append(["C", 100, 90, 96])
    return workbook


def workbook_data(text):
    workbook = fixture_workbook(text)
    content = io.BytesIO()
    workbook.save(content)
    return base64.b64encode(content.getvalue()).decode()


SWITCH_TABS = """
<script>
function selectPanel(button) {
    for (const tab of button.parentElement.children) {
        tab.setAttribute("aria-selected", tab === button);
        document.getElementById(tab.getAttribute("aria-controls")).hidden = true;
    }
    setTimeout(() => {document.getElementById(button.getAttribute("aria-controls")).hidden = false}, 80);
}
</script>
"""


class CampaignRoutes(unittest.TestCase):
    def test_mode_validation_and_older_requests(self):
        payload = {"store": "泰国测试", "campaign": "Demo", "scene": "Weekend offers"}
        defaults = campaign.download_options(payload, AppSettings())
        self.assertIsNone(defaults.mode)
        self.assertEqual((defaults.processing_mode, defaults.threshold), ("auto", Decimal("0.95")))
        for mode in ("invited", "unregistered"):
            self.assertEqual(campaign.download_options({**payload, "mode": mode}, AppSettings()).mode, mode)
        for invalid in ("", "registered", ["invited", "unregistered"]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "活动模式"):
                campaign.download_options({**payload, "mode": invalid}, AppSettings())
        for invalid in ({"processing_mode": "unregistered"}, {"threshold": "95"}, {"threshold": 0},
                        {"threshold": "NaN"}, {"threshold": None}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                campaign.download_options({**payload, **invalid}, AppSettings())

    def test_script_can_select_a_mode_interactively_or_by_argument(self):
        with patch.object(campaign.sys.stdin, "isatty", return_value=True), patch.object(
            campaign.ConfigStore, "load", return_value=AppSettings()
        ), patch.object(campaign, "resolve_browser_config"), patch.object(
            campaign, "run_download", return_value={"success": True}
        ) as run, patch("builtins.input", side_effect=["2", "Weekend offers"]):
            args = ["--store", "泰国测试", "--campaign", "Demo"]
            campaign.main(args)
            self.assertEqual(run.call_args.args[0].mode, "unregistered")
            campaign.main([*args, "--mode", "invited", "--scene", "Weekend offers",
                           "--processing-mode", "plain", "--discount-percent", "92.75%"])
            options = run.call_args.args[0]
            self.assertEqual((options.mode, options.processing_mode, options.threshold),
                             ("invited", "plain", Decimal("0.9275")))

    def test_sites_from_multiline_names_and_country_aliases(self):
        targets = campaign.store_targets({"store_names": "  LZ泰国01 \r\n\nLZ菲律宾02\nlz泰国01\n"
                                          "马来西亚03\n马来04\n印尼05\n印度尼西亚06\n越南07\n新加坡08"})
        self.assertEqual([store.site for store in targets], ["TH", "PH", "MY", "MY", "ID", "ID", "VN", "SG"])
        self.assertEqual(targets[0].name, "LZ泰国01")
        for text, message in (("店铺001", "店铺001"), ("泰国-菲律宾01", "多个站点"), ("\n ", "每行一个")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                campaign.store_targets({"store_names": text})
        self.assertEqual(campaign.store_targets({"store": "旧店名", "site": "ph"})[0].site, "PH")
        with self.assertRaisesRegex(ValueError, "不一致"):
            campaign.store_targets({"store": "泰国01", "site": "ph"})

    def test_store_errors_leave_other_downloads_available(self):
        stores = ["LZ跨境菲律宾01", "LZ跨境泰国02", "LZ跨境泰国03"]
        runtime = Mock()
        runtime.list_browsers.return_value = [{"browserName": name, "browserOauth": name} for name in stores[1:]]
        with tempfile.TemporaryDirectory() as folder:
            options = campaign.download_options({"store_names": stores, "campaign": "A+", "scene": TITLE,
                                                 "output_dir": folder}, AppSettings())
            path = Path(folder) / stores[1] / "TH" / "template.xlsx"
            path.parent.mkdir(parents=True)
            fixture_workbook("原始模板").save(path)
            with patch.object(campaign, "ZiniaoPlaywrightRuntime", return_value=runtime), patch.object(
                campaign, "_download_store", side_effect=[path, RuntimeError("未找到子活动")]
            ), patch.object(campaign.workbooks, "filter_campaign_workbooks") as filter_files:
                result = campaign.run_download(options, campaign.BrowserConfig("公司", "用户", "密码"))
            filter_files.assert_not_called()
            self.assertIsNone(result["processing"])
            self.assertIn("已跳过活动处理", result["completion_message"])
            self.assertEqual(result["failed_store_count"], 2)
            self.assertEqual(result["success_store_count"], 1)
            self.assertIn("未唯一匹配", result["stores"][0]["error"])
            self.assertIn("未找到子活动", result["stores"][2]["error"])
            self.assertEqual(result["stores"][1]["output_file"], str(path))
            self.assertTrue(path.is_file())
            self.assertEqual(result["output_dir"], folder)

    def test_filter_errors_keep_downloads_and_use_only_this_runs_templates(self):
        from tests.test_lz_warehouse_inventory import _xls_bytes

        names = ["泰国一店", "菲律宾二店", "泰国三店"]
        runtime = Mock()
        runtime.list_browsers.return_value = [
            {"browserName": name, "browserOauth": name, "platformName": "Lazada"} for name in names
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            options = campaign.download_options({"store_names": names, "campaign": "Demo", "scene": TITLE,
                                                 "output_dir": folder}, AppSettings())
            unused = root / "之前下载.xlsx"
            fixture_workbook("历史文件").save(unused)
            paths = [root / f"{name}.xlsx" for name in names]
            fixture_workbook(paths[0].stem).save(paths[0])
            paths[1] = paths[1].with_suffix(".xls")
            paths[1].write_bytes(_xls_bytes([["A", 100, 95], ["B", 100, 94.99]], headers=["SKU", "原价", "活动价"]))
            Workbook().save(paths[2])  # Valid download, missing campaign price columns.
            originals = [path.read_bytes() for path in paths]
            with patch.object(campaign, "ZiniaoPlaywrightRuntime", return_value=runtime), patch.object(
                campaign, "_download_store", side_effect=paths
            ), patch.object(campaign.workbooks, "MAX_FILES", 2):
                messages = []
                result = campaign.run_download(options, campaign.BrowserConfig("公司", "测试", "密码"), messages.append)
            self.assertFalse(result["is_complete"])
            self.assertEqual((result["success_store_count"], result["failed_store_count"]), (3, 0))
            processing = result["processing"]
            self.assertEqual((processing["success_count"], processing["failed_count"]), (2, 1))
            self.assertIn("价格表头", result["stores"][2]["processing_error"])
            output = Path(processing["output_dir"])
            self.assertEqual(len(list(output.rglob("*_已处理.xlsx"))), 2)
            self.assertEqual(len(list(output.rglob("Excel折扣筛选汇总与异常明细.xlsx"))), 2)
            with Path(result["stores"][1]["processed_file"]).open("rb") as stream:
                book = load_workbook(stream, read_only=True)
                self.assertEqual(list(book.active.values)[1:], [("A", 100, 95)])
                book.close()
            self.assertTrue(any("公式采用缓存值" in message for message in messages))
            self.assertEqual([path.read_bytes() for path in paths], originals)
            self.assertTrue(unused.is_file())

    def test_same_cross_border_store_selects_requested_site(self):
        site = campaign.Site("sellercenter.lazada.co.th", "sellercenter-th.lazada-seller.cn")
        self.assertEqual(campaign.site_code("菲律宾"), "PH")
        self.assertEqual(campaign.site_code("th"), "TH")
        for url in ("https://gsp.lazada-seller.cn/page/login", ORIGIN, "about:blank"):
            self.assertEqual(campaign.target_host(site, url, "LZ跨境菲律宾示例"), site.cross_border_host)
        self.assertEqual(campaign.target_host(site, "https://" + site.local_host, "示例"), site.local_host)
        with self.assertRaises(ValueError):
            campaign.target_host(site, "https://sellercenter.lazada.com.ph/home", "示例")


class CampaignExportFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def test_popup_navigation_close_before_refresh_and_correct_file(self):
        content = workbook_data(TITLE)

        def respond(route):
            path = urlsplit(route.request.url).path
            if path == "/portal/home":
                html = "Seller Center"
            elif path == campaign.HOME:
                html = '<a target="_blank" href="/portal/campaign/master/detail">A+ Other</a>'
            elif path == "/portal/campaign/master/detail":
                html = f'<a target="_blank" href="{DETAIL}">{TITLE}</a>'
            elif path == DETAIL:
                html = SCENE_HTML.replace("WORKBOOK", content)
            else:
                route.abort()
                return
            route.fulfill(content_type="text/html; charset=utf-8", body=html)

        self.context.route("**/*", respond)
        th_store, ph_store = "LZ跨境泰国示例", "LZ菲律宾示例"
        ph_host = "sellercenter.lazada.com.ph"
        self.page.goto(ORIGIN + "/portal/home")
        ph_page = self.context.new_page()
        ph_page.goto("https://" + ph_host + "/portal/home")
        runtime = Mock()
        catalog = [{"browserName": name, "browserOauth": name, "platformName": "Lazada"}
                   for name in (th_store, ph_store)]
        runtime.list_browsers.return_value = list(reversed(catalog))
        opened = [SimpleNamespace(page=self.page), SimpleNamespace(page=ph_page)]
        runtime.open_browser.side_effect = opened
        self.assertEqual(campaign.REFRESH_SECONDS, 120)
        with tempfile.TemporaryDirectory() as directory:
            options = campaign.download_options({"store_names": [th_store, ph_store], "campaign": "A+ Other",
                                                 "scene": TITLE, "timeout_minutes": 1, "output_dir": directory}, AppSettings())
            with patch.object(campaign, "ZiniaoPlaywrightRuntime", return_value=runtime), patch.object(campaign, "REFRESH_SECONDS", .05):
                batch = campaign.run_download(options, campaign.BrowserConfig("公司", "测试账号", "测试密码"))
            self.assertTrue(batch["success"], batch)
            runtime.start.assert_called_once()
            runtime.list_browsers.assert_called_once()
            self.assertEqual([c.args[0] for c in runtime.open_browser.call_args_list], catalog)
            self.assertEqual(runtime.close_browser.call_args_list, [call(item) for item in opened])
            runtime.shutdown.assert_called_once()
            scenes = {urlsplit(page.url).hostname: page for page in self.context.pages if urlsplit(page.url).path == DETAIL}
            self.assertEqual(set(scenes), {"sellercenter-th.lazada-seller.cn", ph_host})
            for scene in scenes.values():
                self.assertEqual(scene.evaluate("sessionStorage.downloadId"), "12")
                self.assertEqual(scene.evaluate("sessionStorage.exports"), "1")
                self.assertEqual(scene.evaluate("sessionStorage.closes"), "2")
            for row, (name, site) in zip(batch["stores"], [(th_store, "TH"), (ph_store, "PH")], strict=True):
                output = Path(row["output_file"])
                self.assertEqual(output.parent, Path(directory) / name / site)
                with output.open("rb") as stream:
                    result = load_workbook(stream, read_only=True)
                    self.assertEqual(result.active["A1"].value, TITLE)
                    self.assertEqual(result.active.max_row, 5)
                    result.close()
                with Path(row["processed_file"]).open("rb") as stream:
                    filtered = load_workbook(stream, read_only=True)
                    self.assertEqual(list(filtered.active.values)[2:], [("A", 100, 95, 92.75)])
                    filtered.close()
            self.assertEqual(batch["processing"]["success_count"], 2)

    def test_duplicate_scene_name_is_rejected_before_click(self):
        self.page.set_content("<button>sale</button><button>sale</button>")
        with patch.object(campaign, "SCENE_CLICK_DELAY_MS", 10), self.assertRaisesRegex(RuntimeError, "多个位置"):
            campaign.enter_activity(self.page, HOST, "sale", campaign.SCENE)

    def test_scene_waits_for_delayed_list_and_clicks_the_replacement_card(self):
        master = ORIGIN + "/portal/campaign/master/detail"
        self.context.route(ORIGIN + DETAIL, lambda route: route.fulfill(body="Scene detail"))
        self.context.route(master, lambda route: route.fulfill(content_type="text/html", body="""
            <section id="list" aria-busy="true"></section>
            <script>
            const list = document.querySelector('#list');
            window.clicks = [];
            list.addEventListener('click', event => {
                window.clicks.push([event.target.id, list.getAttribute('aria-busy'), performance.now() - window.shown]);
            });
            setTimeout(() => {
                window.shown = performance.now();
                list.innerHTML = '<span>Weekend offers</span>';
                setTimeout(() => {
                    list.innerHTML = '<button id="card" disabled><span style="pointer-events:none">Weekend offers</span></button>';
                    const card = document.querySelector('#card');
                    card.onclick = () => window.open('DETAIL', '_blank');
                    setTimeout(() => {
                        window.readyAt = performance.now() - window.shown;
                        list.setAttribute('aria-busy', 'false');
                        card.disabled = false;
                    }, 250);
                }, 350);
            }, 100);
            </script>""".replace("DETAIL", DETAIL)))
        self.page.goto(master)
        messages = []
        with patch.object(campaign, "SCENE_CLICK_DELAY_MS", 200):
            detail = campaign.enter_activity(self.page, HOST, "Weekend offers", campaign.SCENE,
                                             scope=self.page.locator("#list"), progress=messages.append)
        self.assertEqual(detail.url, ORIGIN + DETAIL)
        self.assertIs(detail.opener(), self.page)
        clicks = self.page.evaluate("window.clicks")
        self.assertEqual(len(clicks), 1)
        control, busy, elapsed_ms = clicks[0]
        self.assertEqual((control, busy), ("card", "false"))
        self.assertGreaterEqual(elapsed_ms, self.page.evaluate("window.readyAt"))
        self.assertTrue(any("已自动点击" in text for text in messages))

    def test_scene_recovers_ignored_click_but_waits_for_an_opening_detail(self):
        master = ORIGIN + "/portal/campaign/master/detail"
        self.context.route(ORIGIN + DETAIL, lambda route: route.fulfill(body="Scene detail"))
        self.context.route(master, lambda route: route.fulfill(content_type="text/html", body="""
            <button id="card">Weekend offers</button>"""))
        neighbor = self.context.new_page()
        neighbor.goto(master)
        neighbor.evaluate("""() => {
            window.channel = new BroadcastChannel('campaign-neighbor');
            channel.onmessage = event => { location.href = event.data; };
        }""")
        with (
            patch.object(campaign, "SCENE_CLICK_DELAY_MS", 150),
            patch.object(campaign, "SCENE_RETRY_AFTER_SECONDS", .25),
            patch.object(campaign, "ACTIVITY_WAIT_MS", 3_000),
        ):
            for behavior, expected in (("ignore-first", 2), ("slow-popup", 1), ("loading-before-popup", 1)):
                with self.subTest(behavior=behavior):
                    self.page.goto(master)
                    self.page.evaluate("""([behavior, path]) => {
                        sessionStorage.clicks = 0;
                        const start = performance.now();
                        document.querySelector('#card').onclick = () => {
                            sessionStorage.clicks = Number(sessionStorage.clicks) + 1;
                            if (sessionStorage.clicks === '1') sessionStorage.firstClickMs = performance.now() - start;
                            if (behavior === 'ignore-first') {
                                if (sessionStorage.clicks === '1') {
                                    new BroadcastChannel('campaign-neighbor').postMessage(path);
                                } else {
                                    location.href = path;
                                }
                            } else if (behavior === 'slow-popup') {
                                const popup = window.open('about:blank', '_blank');
                                setTimeout(() => { popup.location.href = path; }, 800);
                            } else {
                                const card = document.querySelector('#card');
                                card.setAttribute('aria-busy', 'true');
                                setTimeout(() => {
                                    card.setAttribute('aria-busy', 'false');
                                    window.open(path, '_blank');
                                }, 800);
                            }
                        };
                    }""", [behavior, DETAIL])
                    messages = []
                    detail = campaign.enter_activity(self.page, HOST, "Weekend offers", campaign.SCENE,
                                                     progress=messages.append)
                    self.assertEqual(detail.url, ORIGIN + DETAIL)
                    if behavior == "ignore-first":
                        self.assertIs(detail, self.page)
                        self.assertEqual(neighbor.url, ORIGIN + DETAIL)
                    self.assertEqual(int(self.page.evaluate("sessionStorage.clicks")), expected)
                    self.assertGreaterEqual(float(self.page.evaluate("sessionStorage.firstClickMs")), 150)
                    self.assertEqual(sum("重新定位" in text for text in messages), expected - 1)

    def test_selected_modes_download_distinct_files_for_identically_named_scenes(self):
        title = "[Demo] Weekend offers"
        paths = {"invited": "/portal/campaign/scene/detail/101", "unregistered": "/portal/campaign/scene/detail/102"}
        master_tabs = []
        product_tabs = []
        documents = {}
        for mode, tab, product, menu in (("invited", "特别邀请", "Special Invitation", "Accept by Excel"),
                                        ("unregistered", "未报名活动（1）", "待提交", "用Excel提交")):
            link = f'<a target="_blank" href="{paths[mode]}">{title}</a>'
            master_tabs.append((mode, tab, link))
            open_action = "options.hidden=false" if mode == "invited" else "menu.hidden=!menu.hidden"
            trigger = (f'<button onclick="sessionStorage.menus=(sessionStorage.menus||\'\')+\'{mode} \'; '
                       f'{open_action}">{menu}</button>')
            product_tabs.append(("product-" + mode, product, trigger))
            documents[paths[mode]] = workbook_data(mode)

        # The summary and the ineligible tab contain the same title as the two selectable tabs.
        decoy = f'<a href="/portal/campaign/scene/detail/999">{title}</a>'
        master = decoy + tab_group([*master_tabs, ("ineligible", "无资质活动", decoy)], "invited") + SWITCH_TABS
        products = tab_group(product_tabs, "product-unregistered")
        detail = tab_group([("products", "商品提交", products),
                            ("offers", "店铺优惠", '<button>用Excel提交</button>')], "offers") + SWITCH_TABS
        template = SCENE_HTML.replace('<button onclick="menu.hidden=!menu.hidden">用Excel提交</button>', detail)
        template = template.replace(
            'hidden id="options">', 'hidden id="options">'
            '<button onclick="options.hidden=true; historyPanel()">Import/Export History</button>',
        )

        def respond(route):
            path = urlsplit(route.request.url).path
            if path == campaign.HOME:
                html = '<a target="_blank" href="/portal/campaign/master/detail">A+ Other</a>'
            elif path == "/portal/campaign/master/detail":
                html = master
            elif path in documents:
                html = template.replace("WORKBOOK", documents[path])
            else:
                route.abort()
                return
            route.fulfill(content_type="text/html; charset=utf-8", body=html)

        self.context.route("**/*", respond)
        store = "LZ跨境马来测试"
        runtime = Mock()
        runtime.open_browser.return_value = SimpleNamespace(page=self.page)
        with tempfile.TemporaryDirectory() as folder, patch.object(campaign, "REFRESH_SECONDS", .05):
            for mode, path in paths.items():
                with self.subTest(mode=mode):
                    options = campaign.download_options(
                        {"store": store, "mode": mode, "campaign": "A+ Other", "scene": title, "output_dir": folder},
                        AppSettings(),
                    )
                    messages = []
                    output = campaign._download_store(runtime, {"browserName": store}, "MY", options, messages.append)
                    name = campaign.CAMPAIGN_MODES[mode].title
                    self.assertTrue(output.name.startswith(name + "_"))
                    self.assertIn(f"MY 站「A+ Other」下的「{name}」", " ".join(messages))
                    with output.open("rb") as stream:
                        result = load_workbook(stream, read_only=True)
                        self.assertEqual(result.active["A1"].value, mode)
                        result.close()
                    scene = next(page for page in self.context.pages if page.url == ORIGIN + path)
                    self.assertEqual(scene.evaluate("sessionStorage.menus").split(), [mode] * 3)
                    self.assertEqual(scene.evaluate("sessionStorage.exports"), "1")
                    self.assertEqual(scene.evaluate("sessionStorage.closes"), "2")

    def test_only_the_selected_status_is_searched(self):
        self.page.set_content(tab_group([
            ("special", "Special Invitations", '<button>Invitation exclusive</button>'),
            ("available", "Unregistered Campaign", "<p>No campaigns</p>"),
        ], "special") + SWITCH_TABS)
        mode = campaign.CAMPAIGN_MODES["unregistered"]
        root = campaign.select_tab(self.page, campaign.tab_name(mode.master_tab), mode.title)
        with patch.object(campaign, "ACTIVITY_WAIT_MS", 300):
            with self.assertRaisesRegex(RuntimeError, "未找到.*Invitation exclusive"):
                campaign.enter_activity(self.page, HOST, "Invitation exclusive", campaign.SCENE, scope=root)
            self.page.set_content('<button>已报名活动</button><button>Invitation exclusive</button>')
            with self.assertRaisesRegex(RuntimeError, "未找到.*未报名活动"):
                campaign.select_tab(self.page, campaign.tab_name(mode.master_tab), mode.title)

    def test_text_only_tabs_and_shared_excel_menu(self):
        self.page.set_content("""<button onclick="panel.hidden=false"><span>特邀活动</span></button>
            <section id="panel" hidden>示例子活动</section>""")
        mode = campaign.CAMPAIGN_MODES["invited"]
        campaign.select_tab(self.page, campaign.tab_name(mode.master_tab), mode.title)
        expect(self.page.get_by_text("示例子活动")).to_be_visible()
        self.page.set_content("<button>用 Excel 提交</button>")
        self.assertTrue(campaign.excel_menu(self.page, mode).is_visible())

    def test_selected_tab_waits_for_its_labelled_panel(self):
        self.page.set_content("""
            <div role="tablist"><button role="tab" id="inv" aria-selected="true">特别邀请</button>
              <button role="tab" id="reg" aria-selected="false"
                onclick="this.setAttribute('aria-selected', 'true'); setTimeout(showRegular, 250)">未报名活动</button>
            </div>
            <section role="tabpanel" aria-labelledby="inv"><a href="/invited">Weekend</a></section>
            <section role="tabpanel" aria-labelledby="reg" hidden><a href="/regular">Weekend</a></section>
            <script>function showRegular() {
                const panels = document.querySelectorAll('section');
                panels[0].hidden = true; panels[1].hidden = false;
            }</script>""")
        mode = campaign.CAMPAIGN_MODES["unregistered"]
        scope = campaign.select_tab(self.page, campaign.tab_name(mode.master_tab), mode.title)
        self.assertEqual(scope.get_by_text("Weekend").get_attribute("href"), "/regular")

    def test_invitation_dialog_can_download_a_file_directly(self):
        content = workbook_data("邀请示例")
        self.page.set_content(f"""<button onclick="options.hidden=false">通过 Excel 接受</button>
            <div role="dialog" hidden id="options">
              <a download="invitation.xlsx" href="data:application/octet-stream;base64,{content}">下载</a>
            </div>""")
        download = campaign.export_template(self.page, 1, mode=campaign.CAMPAIGN_MODES["invited"])
        self.assertEqual(download.suggested_filename, "invitation.xlsx")
        with download.path().open("rb") as stream:
            result = load_workbook(stream, read_only=True)
            self.assertEqual(result.active["A1"].value, "邀请示例")
            result.close()

    def test_agreement_on_master_auto_opens_or_unlocks_only_the_selected_scene(self):
        master = ORIGIN + "/portal/campaign/master/detail"
        html = AGREEMENT_HTML + """
            <button id="scene">Weekend offers</button><button id="outside">确定</button>
            <script>
            document.querySelector('#outside').onclick = () => sessionStorage.outside = 'clicked';
            document.querySelector('#scene').onclick = () => {
                sessionStorage.sceneClicks = Number(sessionStorage.sceneClicks || 0) + 1;
                if (sessionStorage.agreed) window.open('DETAIL', '_blank');
                else setTimeout(() => { document.querySelector('#terms').hidden = false; }, 80);
            };
            </script>"""
        self.context.route(master, lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                                                              body=html.replace("DETAIL", DETAIL)))
        self.context.route(ORIGIN + DETAIL, lambda route: route.fulfill(body="Scene detail"))
        with patch.object(campaign, "SCENE_CLICK_DELAY_MS", 10), patch.object(campaign, "SCENE_RETRY_AFTER_SECONDS", .4):
            for auto_open in (True, False):
                with self.subTest(auto_open=auto_open):
                    self.page.goto(master)
                    self.page.evaluate("sessionStorage.clear()")
                    if auto_open:
                        self.page.evaluate("path => { window.afterAgreement = () => window.open(path, '_blank'); }", DETAIL)
                    messages = []
                    detail = campaign.enter_activity(self.page, HOST, "Weekend offers", campaign.SCENE,
                                                     progress=messages.append)
                    self.assertEqual(detail.url, ORIGIN + DETAIL)
                    self.assertEqual(self.page.evaluate("JSON.parse(sessionStorage.agreed)"), ["platform", "approved", "locked"])
                    self.assertEqual(self.page.evaluate("sessionStorage.confirmCount"), "1")
                    self.assertEqual(self.page.evaluate("sessionStorage.sceneClicks"), "1" if auto_open else "2")
                    self.assertIsNone(self.page.evaluate("sessionStorage.getItem('outside')"))
                    self.assertTrue(any("必选 2 项" in line for line in messages))
                    detail.close()

    def test_async_agreement_on_detail_resumes_excel_export(self):
        html = AGREEMENT_HTML + '<main id="products" hidden>' + SCENE_HTML + '</main>' + """
            <script>
            window.afterAgreement = () => { document.querySelector('#products').hidden = false; };
            if (sessionStorage.agreed) window.afterAgreement();
            else setTimeout(() => { document.querySelector('#terms').hidden = false; }, 120);
            </script>"""
        content = html.replace("WORKBOOK", workbook_data("Agreement demo"))
        self.context.route(ORIGIN + DETAIL, lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=content))
        self.page.goto(ORIGIN + DETAIL)
        with patch.object(campaign, "REFRESH_SECONDS", .05):
            download = campaign.export_template(self.page, 1, mode=campaign.CAMPAIGN_MODES["unregistered"])
        self.assertEqual(download.suggested_filename, "template.xlsx")
        with download.path().open("rb") as stream:
            workbook = load_workbook(stream, read_only=True)
            self.assertEqual(workbook.active["A1"].value, "Agreement demo")
            workbook.close()
        self.assertEqual(self.page.evaluate("JSON.parse(sessionStorage.agreed)"), ["platform", "approved", "locked"])
        self.assertEqual(self.page.evaluate("[sessionStorage.confirmCount, sessionStorage.exports, sessionStorage.downloadId]"),
                         ["1", "1", "12"])

    def test_optional_only_and_custom_required_agreements(self):
        for mandatory in (False, True):
            with self.subTest(mandatory=mandatory):
                badge = '<span id="badge">Mandatory</span>' if mandatory else 'Not required'
                self.page.set_content(f"""<div role="dialog" aria-label="Campaign Agreements">
                    <div><div id="custom" role="checkbox" aria-checked="false" aria-describedby="badge"
                        onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') !== 'true')">Terms</div>
                        {badge}</div>
                    <label><input id="opt" type="checkbox" checked>Highly Recommended</label>
                    <fieldset><legend>scope <b>Required</b></legend><input type="radio" id="scope" checked></fieldset>
                    <button onclick="window.choices=[custom.getAttribute('aria-checked'), opt.checked, scope.checked];
                        this.closest('[role=dialog]').hidden=true">Confirm</button></div>""")
                confirm_required_agreements(agreement_dialog(self.page), lambda _: None)
                self.assertEqual(self.page.evaluate("window.choices"), [str(mandatory).lower(), False, True])

    def test_rechecks_an_optional_default_restored_by_a_late_render(self):
        self.page.set_content("""<dialog open><div>大促协议</div>
            <label><input type="checkbox" checked required>平台条款</label>
            <label><input type="checkbox" id="offer" checked>额外推广</label>
            <button onclick="window.finalOffer=offer.checked; this.closest('dialog').close()">确定</button>
            </dialog><script>
            window.changes = 0;
            offer.onchange = () => { if (++window.changes === 1) setTimeout(() => offer.checked = true, 320); };
            </script>""")
        confirm_required_agreements(agreement_dialog(self.page), lambda _: None)
        self.assertEqual(self.page.evaluate("[window.finalOffer, window.changes]"), [False, 2])

    def test_existing_direct_download_dialog_is_still_available_without_a_mode(self):
        content = workbook_data("Direct download")
        self.page.set_content(f"""<div role="dialog"><a download="direct.xlsx"
            href="data:application/octet-stream;base64,{content}">下载</a></div>""")
        download = campaign.export_template(self.page, 1)
        self.assertEqual(download.suggested_filename, "direct.xlsx")
        self.assertGreater(download.path().stat().st_size, 0)

    def test_unverifiable_agreements_are_not_confirmed(self):
        for controls, error in (
            ('<section><input type="checkbox"><input type="checkbox"><b>必选</b></section>', "唯一对应"),
            ('<section><b>必选</b></section><section><input type="checkbox">推荐</section>', "唯一对应"),
            ('<label><input type="checkbox" disabled checked>强烈推荐</label>', "无法读取或调整"),
            ('<label><input type="checkbox" aria-required="true" disabled>Platform terms</label>', "无法读取或调整"),
        ):
            with self.subTest(error=error, controls=controls):
                self.page.set_content('<div role="dialog"><h2>大促协议</h2>' + controls +
                    '<button onclick="window.signed=true">确定</button></div>')
                with self.assertRaisesRegex(RuntimeError, error):
                    confirm_required_agreements(agreement_dialog(self.page), lambda _: None, timeout_ms=1200)
                self.assertFalse(self.page.evaluate("Boolean(window.signed)"))
        self.page.set_content('<div role="dialog"><h2>导出设置</h2><a>活动协议</a><label><input type="checkbox">必选</label>'
                              '<button>确定</button></div>')
        self.assertEqual(agreement_dialog(self.page).count(), 0)

    def test_unexpected_history_in_download_only_dialog_never_returns_an_old_file(self):
        self.page.set_content("""<button onclick="options.hidden=false">Accept by Excel</button>
            <div role="dialog" hidden id="options">
              <button onclick="options.hidden=true; historyPanel.hidden=false">Download</button>
            </div>
            <div role="dialog" hidden id="historyPanel"><b>Export Records</b>
              <button onclick="window.oldSelected=true">Download</button>
            </div>""")
        with self.assertRaisesRegex(RuntimeError, "无法确认本次导出任务"):
            campaign.export_template(self.page, 1, mode=campaign.CAMPAIGN_MODES["invited"])
        self.assertFalse(self.page.evaluate("Boolean(window.oldSelected)"))

    def test_export_failure_and_concurrent_jobs_never_download_old_success(self):
        for new_rows, error in (
            ("row(12, '失败')", "导出失败"),
            ("row(12, '成功') + row(13, '生成中')", "多个新任务"),
        ):
            with self.subTest(error=error):
                document = SCENE_HTML.replace("row(12, sessionStorage.ready ? '成功' : '生成中')", new_rows)
                self.context.route(ORIGIN + DETAIL, lambda route: route.fulfill(
                    content_type="text/html; charset=utf-8", body=document))
                self.page.goto(ORIGIN + DETAIL)
                self.page.evaluate("sessionStorage.clear()")
                with self.assertRaisesRegex(RuntimeError, error):
                    campaign.export_template(self.page, 1)
                self.assertIsNone(self.page.evaluate("sessionStorage.getItem('downloadId')"))
                self.assertEqual(self.page.evaluate("sessionStorage.exports"), "1")
                self.context.unroute(ORIGIN + DETAIL)

    def test_desktop_form_starts_and_restores_its_download(self):
        bridge = AppBridge()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        account = BoundAccount("z1", "ziniao", "演示公司", "fixture-user", "fixture-password")
        bridge.config_store = Mock()
        bridge.config_store.load.return_value = AppSettings(
            output_dir=directory.name, accounts=[account], active_account_ids={"ziniao": account.id})
        bridge.get_sidebar_preferences = Mock(return_value={"ok": True, "preferences": {}})
        bridge.reveal_path = Mock(return_value={"ok": True})
        allowed = {"get_app_info", "get_sidebar_preferences", "get_latest_task_status", "get_task_status",
                   "get_lazada_campaign_download_info", "start_lazada_campaign_download", "reveal_path"}

        def invoke(method, args):
            self.assertIn(method, allowed)
            return getattr(bridge, method)(*args)

        self.context.expose_function("invokeDesktop", invoke)
        self.context.add_init_script("""window.pywebview = {api: new Proxy({}, {
            get: (_, method) => (...args) => window.invokeDesktop(method, args)
        })};""")
        finish = threading.Event()
        store_names = ["LZ跨境泰国示例", "LZ跨境菲律宾示例"]
        runtime = Mock()
        runtime.list_browsers.return_value = [{"browserName": name, "browserOauth": name} for name in store_names]

        def simulated_download(_runtime, browser, site, options, progress):
            progress("正在导出子活动的 Excel 模板")
            if site == "PH" and not finish.wait(25):
                raise RuntimeError("测试等待超时")
            directory = Path(options.output_dir) / browser["browserName"] / site
            directory.mkdir(parents=True)
            path = directory / "活动模板.xlsx"
            fixture_workbook(browser["browserName"]).save(path)
            return path

        pages = Path(__file__).resolve().parents[1] / "frontend" / "pages"
        with patch("backend.app_bridge.get_group_options", return_value=[]), patch.object(
            campaign, "ZiniaoPlaywrightRuntime", return_value=runtime
        ) as session, patch.object(campaign, "_download_store", side_effect=simulated_download) as runner:
            self.page.goto((pages / "dashboard.html").as_uri())
            self.page.locator('a.dashboard-tool[href="lazada-campaign-download.html"]').click()
            expect(self.page.locator("#downloadBtn")).to_be_enabled()
            store_field = self.page.locator("textarea#store")
            expect(self.page.locator("#site")).to_have_count(0)
            store_field.fill("泰国示例\n未知店铺\n泰国菲律宾示例")
            expect(self.page.locator("#siteHint")).to_contain_text("第 2 行「未知店铺」未识别")
            expect(self.page.locator("#siteHint")).to_contain_text("第 3 行「泰国菲律宾示例」包含多个")
            self.assertFalse(store_field.evaluate("el => el.checkValidity()"))
            store_field.fill("\n".join([*store_names, "", store_names[0]]))
            expect(self.page.locator("#siteHint")).to_contain_text("泰国（TH）1 家、菲律宾（PH）1 家")
            self.page.locator("#campaign").fill("A+ Other")
            mode_field = self.page.get_by_label("活动模式")
            expect(mode_field).to_have_value("invited")
            expect(mode_field.locator("option")).to_have_text(["特邀活动（特别邀请）", "未报名活动"])
            mode_field.select_option("unregistered")
            self.page.locator("#scene").fill(TITLE)
            processing = self.page.get_by_label("处理方式")
            percent = self.page.get_by_role("spinbutton", name="最低折扣率", exact=True)
            expect(processing).to_have_value("auto")
            expect(percent).to_have_value("95")
            processing.select_option("invited")
            for invalid in ("", "0", "100.01"):
                percent.fill(invalid)
                self.assertFalse(percent.evaluate("input => input.checkValidity()"))
            percent.fill("92.75")
            root = Path(directory.name) / "Lazada活动模板"
            try:
                self.page.locator("#downloadBtn").click()
                expect(self.page.locator("#taskBadge")).to_have_text("运行中")
                expect(self.page.locator("#logBox")).to_contain_text(str(root / store_names[0] / "TH" / "活动模板.xlsx"))
                self.assertEqual(list(root.glob("Excel折扣筛选*")), [])
                self.page.reload()
                expect(store_field).to_have_value("\n".join(store_names))
                expect(self.page.locator("#siteHint")).to_contain_text("泰国（TH）1 家、菲律宾（PH）1 家")
                expect(mode_field).to_have_value("unregistered")
                expect(mode_field).to_be_disabled()
                expect(processing).to_have_value("invited")
                expect(percent).to_have_value("92.75")
                expect(processing).to_be_disabled()
                expect(percent).to_be_disabled()
                expect(self.page.locator("#openProcessedBtn")).to_be_disabled()
                expect(self.page.locator("#scene")).to_have_value(TITLE)
                expect(self.page.locator("#downloadBtn")).to_be_disabled()
            finally:
                finish.set()
            expect(self.page.locator("#taskBadge")).to_have_text("已完成")
            for name, site in zip(store_names, ["TH", "PH"]):
                path = root / name / site / "活动模板.xlsx"
                expect(self.page.locator("#logBox")).to_contain_text(str(path))
                self.assertTrue(path.is_file())
            self.page.locator("#openFolderBtn").click()
            bridge.reveal_path.assert_called_once_with(str(root))
            self.assertEqual(runner.call_count, 2)
            self.assertTrue(all(entry.args[3].mode == "unregistered" for entry in runner.call_args_list))
            self.assertEqual(session.call_args.args[0].username, account.username)
            snapshot = bridge.get_latest_task_status("lazada_campaign_download")
            self.assertNotIn(account.password, str(snapshot))
            self.assertEqual(snapshot["context"]["threshold"], "0.9275")
            saved = snapshot["result"]["processing"]
            self.assertEqual((saved["success_count"], saved["failed_count"]), (2, 0))
            result_dir = Path(saved["output_dir"])
            self.assertTrue((result_dir / "Excel折扣筛选汇总与异常明细.xlsx").is_file())
            self.assertEqual(list(result_dir.glob("*.zip")), [])
            for store in snapshot["result"]["stores"]:
                with Path(store["processed_file"]).open("rb") as stream:
                    book = load_workbook(stream, read_only=True)
                    self.assertEqual(book.active["A1"].value, store["store"])
                    self.assertEqual(list(book.active.values)[2:], [("A", 100, 92.75, 92.75), ("C", 100, 96, 96)])
                    book.close()
            self.page.reload()
            expect(percent).to_have_value("92.75")
            self.page.locator("#openProcessedBtn").click()
            bridge.reveal_path.assert_called_with(str(result_dir))


if __name__ == "__main__":
    unittest.main()
