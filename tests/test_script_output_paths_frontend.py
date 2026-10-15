"""Exercise isolated script paths and country changes through real browser events."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:
    sync_playwright = None

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
BASE = "C:/Desktop/log"
COUNTRIES = {"TH": "泰国", "PH": "菲律宾", "MY": "马来西亚", "ID": "印尼", "VN": "越南"}


class ScriptOutputPathsFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is required")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        for options in ({"channel": "msedge"}, {}):
            try:
                cls.browser = cls.playwright.chromium.launch(headless=True, **options)
                break
            except PlaywrightError:
                continue
        else:
            raise unittest.SkipTest("Chromium/Edge is required")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def open_page(self, module, scopes=None, latest_task=None):
        self.page.goto("about:blank")
        html = (FRONTEND / "pages" / f"{module}.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>|<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""({scopes, latestTask}) => {
          window.scopes = scopes || {}; window.savedPaths = []; window.chooserCalls = []; window.sharedSaves = [];
          window.globalChooserCalls = 0; window.deferSaves = false; window.pendingSaves = []; window.deferChooser = false; window.pendingChoosers = [];
          window.latestTask = latestTask || {ok:true,empty:true};
          const countries = [['TH','泰国'],['PH','菲律宾'],['MY','马来西亚'],['ID','印尼'],['VN','越南']];
          const pathFor = (tool,country='') => {
            const saved = window.scopes[`${tool}:${country}`] || {};
            const name = countries.find(item=>item[0]===country)?.[1] || '泰国';
            return {...(tool==='lazada_bill_detail' ? {output_dir:`C:/Desktop/log/Lazada${name}账单明细`,screenshot_root:`C:/Desktop/log/Lazada${name}账单明细截图`}
              : {output_dir:tool==='lazada_ads_data'?'C:/Desktop/log/lz泰国广告数据':'C:/Desktop/log/lz各仓库存'}),...saved};
          };
          const accounts=[{id:'mb',vendor:'mabang',name:'测试马帮',username:'fixture-mb'},{id:'z',vendor:'ziniao',name:'测试紫鸟',username:'fixture-z'}];
          window.pywebview={api:{
            async get_app_info(){return {ok:true,app:{name:'测试',version:'test'},settings:{output_dir:'C:/Desktop/log/lz各仓库存',dingtalk:{operator_names:['测试操作人']}},account_state:{accounts,active_account_ids:{mabang:'mb',ziniao:'z'}}};},
            async get_sidebar_preferences(){return {ok:true,preferences:{}};},
            async get_latest_task_status(tool){return tool?window.latestTask:{ok:true,empty:true};},
            async get_lz_warehouse_inventory_info(){return {ok:true,account_id:'mb',account_name:'测试马帮',...pathFor('lz_warehouse_inventory'),warehouses:['泰国TH04仓']};},
            async get_lz_warehouse_inventory_warehouses(){return {ok:true,warehouses:[{id:'W1',name:'泰国TH04仓'}]};},
            async get_lazada_ads_data_info(){return {ok:true,...pathFor('lazada_ads_data','TH'),client_path:'C:/Ziniao.exe',workbook_id:'fixture-book',saved_preferences:{client_path:'C:/Ziniao.exe',workbook_id:'fixture-book'},operator_names:['测试操作人'],default_sheet_name:'26年10月'};},
            async get_lazada_bill_detail_info(){return {ok:true,countries:countries.map(([code,name])=>({code,name,...pathFor('lazada_bill_detail',code)})),...pathFor('lazada_bill_detail','TH'),client_path:'C:/Ziniao.exe',workbook_id:'fixture-book',default_sheet_name:'26年10月',date_range:{start_date:'2026-09-01',end_date:'2026-09-30'},operator_names:['测试操作人'],dws_available:true};},
            async save_lazada_ads_preferences(payload){sharedSaves.push(payload);return {ok:true};},
            async save_lazada_bill_preferences(payload){sharedSaves.push(payload);return {ok:true};},
            async list_lazada_bill_sheets(){return {ok:true,sheet_names:['泰国','菲律宾','马来','印尼','越南'],default_sheet_name:'26年10月'};},
            async choose_output_dir(){globalChooserCalls++;return {ok:true,path:'C:/Wrong/Global'};},
            async choose_scoped_output_dir(payload){chooserCalls.push({...payload});const value={ok:true,path:`C:/Chosen/${payload.country||'script'}/${payload.kind}`};const finish=()=>{const key=`${payload.tool}:${payload.country||''}`;window.scopes[key]={...(window.scopes[key]||{}),[payload.kind==='screenshot'?'screenshot_root':'output_dir']:value.path};return value;};return deferChooser?new Promise(resolve=>pendingChoosers.push(()=>resolve(finish()))):finish();},
            async save_scoped_output_dirs(payload){savedPaths.push({...payload});const apply=()=>{const key=`${payload.tool}:${payload.country||''}`;window.scopes[key]={...(window.scopes[key]||{}),...Object.fromEntries(Object.entries(payload).filter(([key])=>['output_dir','screenshot_root'].includes(key)))};return {ok:true};};return deferSaves?new Promise(resolve=>pendingSaves.push(()=>resolve(apply()))):apply();},
          }};
        }""", {"scopes": scopes, "latestTask": latest_task})
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages" / f"{module}.js"))
        if module == "lz-warehouse-realtime-inventory":
            expect(self.page.locator("#lzWarehouseStatus")).to_contain_text("已加载")
        elif module == "lazada-bill-detail":
            expect(self.page.locator("#lazadaBillCountry option")).to_have_count(5)
        else:
            expect(self.page.locator("#lazadaAdsWorkbookId")).to_have_value("fixture-book")

    def test_inventory_uses_its_script_default_and_does_not_restore_historical_global_path(self):
        old = {"ok": True, "id": "old", "status": "success", "logs": [], "context": {"account_id": "mb", "output_root": "C:/Old/Shared"}, "result": {"output_dir": "C:/Old/Shared", "output_root": "C:/Old/Shared", "output_files": [], "exports": [], "failures": [], "is_complete": True}}
        self.open_page("lz-warehouse-realtime-inventory", scopes={"lz_warehouse_inventory:": {"output_dir": "C:/Separate/Inventory"}}, latest_task=old)
        self.page.wait_for_function("HQYL.state.taskId === 'old'")
        expect(self.page.locator("#lzOutputDir")).to_have_value("C:/Separate/Inventory")
        self.assertEqual(self.errors, [])

    def test_bill_five_country_changes_immediately_switch_both_paths(self):
        self.open_page("lazada-bill-detail")
        for code, name in COUNTRIES.items():
            self.page.locator("#lazadaBillCountry").select_option(code)
            expect(self.page.locator("#lazadaBillOutputDir")).to_have_value(f"{BASE}/Lazada{name}账单明细")
            expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value(f"{BASE}/Lazada{name}账单明细截图")
            hint = self.page.locator("#lazadaBillScreenshotHint").inner_text()
            self.assertEqual(hint.count(f"Lazada{name}账单明细截图"), 1)
            self.assertIn("2026-09-01到2026-09-30", hint)
        self.assertEqual(self.errors, [])

    def test_three_pages_use_independent_defaults_when_global_points_to_inventory(self):
        for module, field, expected in (
            ("lazada-ads-data", "lazadaAdsOutputDir", f"{BASE}/lz泰国广告数据"),
            ("lazada-bill-detail", "lazadaBillOutputDir", f"{BASE}/Lazada泰国账单明细"),
            ("lz-warehouse-realtime-inventory", "lzOutputDir", f"{BASE}/lz各仓库存"),
        ):
            self.open_page(module)
            expect(self.page.locator(f"#{field}")).to_have_value(expected)
        self.assertEqual(self.errors, [])

    def test_all_directory_choosers_use_scoped_api_and_manual_output_survives_reopen(self):
        for module, button, tool, country in (
            ("lazada-ads-data", "lazadaAdsChooseOutputBtn", "lazada_ads_data", "TH"),
            ("lz-warehouse-realtime-inventory", "lzChooseDirBtn", "lz_warehouse_inventory", ""),
        ):
            self.open_page(module)
            self.page.locator(f"#{button}").click()
            self.page.wait_for_function("savedPaths.length >= 1")
            call = self.page.evaluate("chooserCalls[0]")
            self.assertEqual(call["tool"], tool)
            self.assertEqual(call.get("country", ""), country)
            self.assertEqual(call["kind"], "output")
            self.assertEqual(self.page.evaluate("globalChooserCalls"), 0)
        self.open_page("lazada-ads-data")
        self.page.locator("#lazadaAdsOutputDir").fill("D:/Custom/Ads")
        self.page.locator("#lazadaAdsOutputDir").dispatch_event("change")
        self.page.wait_for_function("scopes['lazada_ads_data:TH']?.output_dir === 'D:/Custom/Ads'")
        saved = self.page.evaluate("scopes")
        self.open_page("lazada-ads-data", scopes=saved)
        expect(self.page.locator("#lazadaAdsOutputDir")).to_have_value("D:/Custom/Ads")
        self.assertEqual(self.errors, [])

    def test_bill_custom_paths_and_delayed_saves_stay_with_the_edited_country(self):
        self.open_page("lazada-bill-detail")
        self.page.evaluate("deferSaves=true")
        self.page.locator("#lazadaBillOutputDir").fill("D:/Custom/TH/Results")
        self.page.locator("#lazadaBillOutputDir").dispatch_event("change")
        self.page.wait_for_function("pendingSaves.length === 1")
        self.page.locator("#lazadaBillCountry").select_option("PH")
        expect(self.page.locator("#lazadaBillOutputDir")).to_have_value(f"{BASE}/Lazada菲律宾账单明细")
        self.page.locator("#lazadaBillScreenshotDir").fill("D:/Custom/PH/Shots")
        self.page.locator("#lazadaBillScreenshotDir").dispatch_event("change")
        self.page.locator("#lazadaBillCountry").select_option("MY")
        expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value(f"{BASE}/Lazada马来西亚账单明细截图")
        self.page.evaluate("() => {deferSaves=false;pendingSaves.shift()();}")
        self.page.wait_for_function("savedPaths.length === 2 && scopes['lazada_bill_detail:PH']?.screenshot_root === 'D:/Custom/PH/Shots'")
        self.assertEqual(self.page.evaluate("savedPaths.map(item=>item.country)"), ["TH", "PH"])
        self.page.locator("#lazadaBillCountry").select_option("TH")
        expect(self.page.locator("#lazadaBillOutputDir")).to_have_value("D:/Custom/TH/Results")
        self.page.locator("#lazadaBillCountry").select_option("PH")
        expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value("D:/Custom/PH/Shots")
        self.assertFalse(any("screenshot_root" in item for item in self.page.evaluate("sharedSaves")))
        saved = self.page.evaluate("scopes")
        self.open_page("lazada-bill-detail", scopes=saved)
        self.page.locator("#lazadaBillCountry").select_option("TH")
        expect(self.page.locator("#lazadaBillOutputDir")).to_have_value("D:/Custom/TH/Results")
        self.page.locator("#lazadaBillCountry").select_option("PH")
        expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value("D:/Custom/PH/Shots")
        self.assertEqual(self.errors, [])

    def test_bill_delayed_picker_keeps_its_country_without_overwriting_new_country_inputs(self):
        self.open_page("lazada-bill-detail")
        self.page.evaluate("deferChooser=true")
        self.page.locator("#lazadaBillChooseScreenshotBtn").click()
        self.page.wait_for_function("pendingChoosers.length === 1")
        self.page.locator("#lazadaBillCountry").select_option("VN")
        self.page.evaluate("pendingChoosers.shift()()")
        self.page.wait_for_function("scopes['lazada_bill_detail:TH']?.screenshot_root === 'C:/Chosen/TH/screenshot'")
        expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value(f"{BASE}/Lazada越南账单明细截图")
        self.page.locator("#lazadaBillCountry").select_option("TH")
        expect(self.page.locator("#lazadaBillScreenshotDir")).to_have_value("C:/Chosen/TH/screenshot")
        self.assertEqual(self.page.evaluate("globalChooserCalls"), 0)
        self.assertEqual(self.errors, [])

    def test_late_picker_response_cannot_overwrite_a_newer_manual_directory(self):
        for module, field, button, scope, value in (
            ("lazada-ads-data", "lazadaAdsOutputDir", "lazadaAdsChooseOutputBtn", "lazada_ads_data:TH", "D:/Latest/Ads"),
            ("lz-warehouse-realtime-inventory", "lzOutputDir", "lzChooseDirBtn", "lz_warehouse_inventory:", "D:/Latest/Inventory"),
            ("lazada-bill-detail", "lazadaBillOutputDir", "lazadaBillChooseOutputBtn", "lazada_bill_detail:TH", "D:/Latest/Bill"),
        ):
            self.open_page(module)
            self.page.evaluate("deferChooser=true")
            self.page.locator(f"#{button}").click()
            self.page.wait_for_function("pendingChoosers.length === 1")
            self.page.locator(f"#{field}").fill(value)
            self.page.locator(f"#{field}").dispatch_event("change")
            self.page.wait_for_function("({scope,value}) => scopes[scope]?.output_dir === value", arg={"scope": scope, "value": value})
            self.page.evaluate("pendingChoosers.shift()()")
            self.page.wait_for_function("({scope,value}) => scopes[scope]?.output_dir === value", arg={"scope": scope, "value": value})
            expect(self.page.locator(f"#{field}")).to_have_value(value)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
