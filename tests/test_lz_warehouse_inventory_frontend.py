"""Exercise the actual lz inventory page and task lifecycle in Chromium offline."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class LzWarehouseInventoryFrontendTests(unittest.TestCase):
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

    def open_page(self, missing_account=False, restored_task=None, load_failure=False, initial_account="target", defer_info_ids=None, defer_catalog_ids=None, wait_for_load=True, info_failure="", catalog_failure="", authentication_failure=""):
        self.page.goto("about:blank")
        html = (FRONTEND / "pages/lz-warehouse-realtime-inventory.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>|<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""({missingAccount, restoredTask, loadFailure, initialAccount, deferInfoIds, deferCatalogIds, infoFailure, catalogFailure, authenticationFailure}) => {
          window.submitted = []; window.selected = []; window.opened = []; window.revealed = [];
          window.startFailure = ''; window.latestTask = restoredTask || {ok:true, empty:true};
          window.warehouseLoadFailure = loadFailure; window.warehouseLoads = 0;
          window.infoRequests=[]; window.catalogRequests=[]; window.deferInfoIds=deferInfoIds||[]; window.deferCatalogIds=deferCatalogIds||[];
          window.pendingInfo=[]; window.pendingCatalogs=[]; window.deferStarts=false; window.pendingStarts=[]; window.deferPolls=false; window.pendingPolls=[];
          window.infoFailure=infoFailure;window.catalogFailure=catalogFailure;window.authenticationFailure=authenticationFailure;
          window.warehouseOptions = ['泰国TH04仓','泰国TH05仓','泰国TH06仓','菲律宾PH01仓'].map((name,index)=>({id:`W-${index}`,name}));
          const accounts = missingAccount ? [] : [
            {id:'other',vendor:'mabang',name:'测试账号B',username:'fixture-user-b'},
            {id:'target',vendor:'mabang',name:'测试账号A',username:'fixture-user-a'},
            {id:'ziniao',vendor:'ziniao',name:'测试紫鸟',username:'fixture-z'},
          ];
          let activeId = missingAccount ? '' : initialAccount;
          window.switchMabang = id => {activeId=id;HQYL.applyAccountState({accounts,active_account_ids:{mabang:id}});};
          window.pywebview = {api: {
            async get_app_info() { return {ok:true, app:{name:'测试',version:'test'}, settings:{output_dir:'C:/Exports'},
              account_state:{accounts,active_account_ids:{mabang:activeId}}}; },
            async get_sidebar_preferences() { return {ok:true,preferences:{}}; },
            async get_lz_warehouse_inventory_info(payload={}) {
              infoRequests.push({...payload}); const id=payload.account_id||activeId;const account=accounts.find(item=>item.id===id&&item.vendor==='mabang');
              if(window.infoFailure) throw new TypeError(window.infoFailure);
              const result=account?{ok:true,account_id:id,account_name:account.name,output_dir:'C:/Exports',warehouses:['泰国TH04仓','泰国TH05仓','泰国TH06仓']}:{ok:false,requires_account:true,vendor:'mabang',error:'请先绑定并选择马帮账号'};
              return window.deferInfoIds.includes(id)?new Promise(resolve=>pendingInfo.push({id,resolve:()=>resolve(result)})):result;
            },
            async get_lz_warehouse_inventory_warehouses(payload={}) {
              warehouseLoads++;catalogRequests.push({...payload});const id=payload.account_id||activeId;
              if(window.catalogFailure) throw new TypeError(window.catalogFailure);
              if(window.authenticationFailure) return {ok:false,error:window.authenticationFailure};
              const options=id==='other'?[{id:'B1',name:'B独有仓'},{id:'B2',name:'泰国TH06仓'}]:warehouseOptions;
              const result=warehouseLoadFailure?{ok:false,error:'目录接口暂时不可用'}:{ok:true,account_id:id,warehouses:options};
              return window.deferCatalogIds.includes(id)?new Promise(resolve=>pendingCatalogs.push({id,resolve:()=>resolve(result)})):result;
            },
            async select_account(vendor,id) { selected.push({vendor,id}); activeId=id; return {ok:true,account_state:{accounts,active_account_ids:{mabang:id}}}; },
            async add_account(payload) { const account={id:'bound',vendor:payload.vendor,name:payload.name||'新绑定账号',username:payload.username};accounts.push(account);activeId=account.id;return {ok:true,account_state:{accounts,active_account_ids:{mabang:activeId}}}; },
            async get_latest_task_status(tool) { return tool === 'lz_warehouse_inventory' ? latestTask : {ok:true,empty:true}; },
            async start_lz_warehouse_inventory(payload) { submitted.push(payload); if(startFailure) return {ok:false,error:startFailure};
              latestTask={ok:true,id:`lz-task-${submitted.length}`,tool:'lz_warehouse_inventory',status:'running',logs:[`${payload.account_id} 正在导出第 1 / 3 页`],context:payload}; const task=latestTask;
              return deferStarts?new Promise(resolve=>pendingStarts.push(()=>resolve(task))):task; },
            async get_task_status(id) { return deferPolls?new Promise(resolve=>pendingPolls.push({id,resolve})):latestTask; },
            async choose_output_dir() { return {ok:true,path:'C:/Chosen'}; },
            async choose_scoped_output_dir() { return {ok:true,path:'C:/Chosen'}; },
            async save_scoped_output_dirs() { return {ok:true}; },
            async open_path(path) { opened.push(path); return {ok:true}; },
            async reveal_path(path) { revealed.push(path); return {ok:true}; },
          }};
        }""", {"missingAccount": missing_account, "restoredTask": restored_task, "loadFailure": load_failure, "initialAccount": initial_account, "deferInfoIds": defer_info_ids, "deferCatalogIds": defer_catalog_ids, "infoFailure": info_failure, "catalogFailure": catalog_failure, "authenticationFailure": authentication_failure})
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages/lz-warehouse-realtime-inventory.js"))
        if missing_account:
            expect(self.page.locator("#lzAccountNote")).to_contain_text("请先绑定")
        elif wait_for_load:
            expect(self.page.locator("#lzAccountNote")).to_contain_text("测试账号B" if initial_account == "other" else "测试账号A")
            expect(self.page.locator("#lzWarehouseStatus")).to_contain_text("加载失败" if load_failure else "已加载")

    def result(self, count=151):
        names = ["泰国TH04仓", "泰国TH05仓", "泰国TH06仓"]
        columns = ["库存SKU", "商品中文名称", "仓库", "可用库存量"]
        exports = [{
            "warehouse_name": name, "output_file": f"C:/Chosen/lz各仓实时库存_{name}_20261015.xlsx",
            "record_count": count if index == 0 else 0, "source_record_count": count if index == 0 else 0,
            "export_page_count": 1, "preview_columns": columns,
            "rows": [{"库存SKU": f"SKU-{row}", "商品中文名称": '<img src=x onerror="window.injected=true">', "仓库": name, "可用库存量": 0} for row in range(min(count, 100))] if index == 0 else [],
        } for index, name in enumerate(names)]
        return {
            "output_file": exports[0]["output_file"], "output_files": [item["output_file"] for item in exports], "output_dir": "C:/Chosen", "output_root": "C:/Chosen",
            "record_count": count, "source_record_count": count, "export_page_count": 3, "warehouse_count": 3,
            "selected_warehouse_count": 3, "exports": exports, "failures": [], "is_complete": True,
            "warehouse_counts": {"泰国TH04仓": count, "泰国TH05仓": 0, "泰国TH06仓": 0},
            "preview_columns": columns, "rows": exports[0]["rows"],
            "preview_truncated": count > 100,
        }

    def test_initially_selected_account_is_kept_without_switching_to_another_bound_account(self):
        self.open_page(initial_account="other")
        self.assertEqual(self.page.evaluate("selected"), [])
        expect(self.page.locator('[data-active-account-name][data-vendor="mabang"]')).to_contain_text("测试账号B")
        self.assertEqual(self.page.evaluate("infoRequests[0]"), {"account_id": "other"})
        self.assertEqual(self.page.evaluate("catalogRequests[0]"), {"account_id": "other"})

    def test_uses_current_account_chooses_directory_and_runs_the_api(self):
        self.open_page()
        self.assertEqual(self.page.evaluate("selected"), [])
        expect(self.page.locator('[data-active-account-name][data-vendor="mabang"]')).to_contain_text("测试账号A")
        self.page.locator("#lzChooseDirBtn").click()
        expect(self.page.locator("#lzOutputDir")).to_have_value("C:/Chosen")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0]"), {"account_id": "target", "output_dir": "C:/Chosen", "warehouse_names": ["泰国TH04仓", "泰国TH05仓", "泰国TH06仓"]})
        self.assertEqual(self.page.evaluate("warehouseLoads"), 1)
        for selector in ("#lzRunBtn", "#lzOutputDir", "#lzChooseDirBtn", "#lzRefreshWarehousesBtn", "#lzWarehouseSearch", "#lzSelectWarehousesBtn", "#lzClearWarehousesBtn"):
            expect(self.page.locator(selector)).to_be_disabled()
        expect(self.page.locator("#lzWarehouseChecklist input").first).to_be_disabled()
        expect(self.page.locator("#logBox")).to_contain_text("第 1 / 3 页")
        self.assertEqual(self.errors, [])

    def test_polling_renders_original_columns_counts_safe_preview_and_opens_result(self):
        self.open_page()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        result = self.result()
        self.page.evaluate("result => { latestTask={...latestTask,status:'success',result}; }", result)
        expect(self.page.locator("#taskBadge")).to_have_text("已完成")
        expect(self.page.locator("#recordCount")).to_have_text("151")
        expect(self.page.locator("#sourceCount")).to_have_text("查询结果 151 条")
        expect(self.page.locator("#exportPageCount")).to_have_text("3")
        expect(self.page.locator("#warehouseCount")).to_have_text("3 个仓库")
        self.assertEqual(self.page.locator("#lzTableHead th").all_text_contents(), result["preview_columns"])
        expect(self.page.locator("#lzTableBody tr")).to_have_count(100)
        expect(self.page.locator("#lzTableBody img")).to_have_count(0)
        expect(self.page.locator("#lzTableBody tr").first).to_contain_text("<img src=x")
        expect(self.page.locator("#lzPreviewNote")).to_have_text("泰国TH04仓：预览前 100 条，完整 151 条见 Excel")
        expect(self.page.locator("#lzOutputFile")).to_have_text(result["output_file"])
        expect(self.page.locator("#lzFileStatus")).to_have_text("3 个文件")
        self.page.locator("#lzOpenFileBtn").click()
        self.page.locator("#lzOpenOutputBtn").click()
        self.assertEqual(self.page.evaluate("opened"), [result["output_file"]])
        self.assertEqual(self.page.evaluate("revealed"), [result["output_file"]])
        expect(self.page.locator("#lzExportFilesBody tr")).to_have_count(3)
        self.page.locator("#lzPreviewWarehouse").select_option("1")
        expect(self.page.locator("#lzOutputFile")).to_have_text(result["exports"][1]["output_file"])
        expect(self.page.locator("#lzEmpty")).to_be_visible()
        self.page.locator("#lzOpenFileBtn").click()
        self.page.locator('[data-lz-open-file="2"]').click()
        self.assertEqual(self.page.evaluate("opened"), result["output_files"])
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_missing_account_blocks_start_and_failure_is_visible(self):
        self.open_page(missing_account=True)
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("submitted"), [])
        self.open_page()
        self.page.evaluate("startFailure='库存导出接口返回错误'")
        self.page.locator("#lzRunBtn").click()
        expect(self.page.locator("#taskBadge")).to_have_text("启动失败")
        expect(self.page.locator("#logBox")).to_contain_text("库存导出接口返回错误")
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_wide_original_fields_scroll_inside_the_preview(self):
        self.open_page()
        self.page.set_viewport_size({"width": 900, "height": 900})
        columns = [f"库存统计原始字段-{index}" for index in range(25)]
        result = self.result(1)
        result["preview_columns"] = columns
        result["rows"] = [{column: "商品库存数据" for column in columns}]
        result["exports"][0]["preview_columns"] = columns
        result["exports"][0]["rows"] = result["rows"]
        self.page.evaluate("result => HQYL.state.page.applyResult(result)", result)
        self.assertEqual(self.page.locator("#lzTableHead th").all_text_contents(), columns)
        self.assertTrue(self.page.locator("#lzTableWrap").evaluate("node => node.scrollWidth > node.clientWidth"))
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(self.errors, [])

    def test_repeated_submit_during_startup_and_running_creates_only_one_task(self):
        self.open_page()
        self.page.evaluate("() => { const form=document.getElementById('lzInventoryForm'); form.requestSubmit(); form.requestSubmit(); }")
        self.page.wait_for_function("submitted.length === 1")
        self.page.evaluate("document.getElementById('lzInventoryForm').requestSubmit()")
        self.assertEqual(len(self.page.evaluate("submitted")), 1)
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_restores_only_designated_account_and_shows_empty_result(self):
        restored = {"ok": True, "id": "restored", "status": "success", "logs": ["完成"], "context": {"account_id": "target", "output_root": "C:/Chosen"}, "result": self.result(0)}
        self.open_page(restored_task=restored)
        expect(self.page.locator("#lzResult")).to_be_visible()
        expect(self.page.locator("#lzEmpty")).to_be_visible()
        expect(self.page.locator("#lzOutputDir")).to_have_value("C:/Exports")
        self.assertEqual(self.page.locator("#lzTableBody tr").count(), 0)
        restored["context"]["account_id"] = "other"
        self.open_page(restored_task=restored)
        expect(self.page.locator("#lzResult")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_search_select_clear_and_submit_only_the_chosen_warehouse(self):
        self.open_page()
        expect(self.page.locator("#lzWarehouseChecklist input:checked")).to_have_count(3)
        self.page.locator("#lzWarehouseSearch").fill("菲律宾")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(1)
        self.page.locator("#lzSelectWarehousesBtn").click()
        expect(self.page.locator("#lzWarehouseSelection")).to_have_text("已选择 4 / 4 个仓库")
        self.page.locator("#lzClearWarehousesBtn").click()
        expect(self.page.locator("#lzWarehouseSelection")).to_have_text("已选择 3 / 4 个仓库")
        self.page.locator("#lzWarehouseSearch").fill("")
        self.page.locator("#lzClearWarehousesBtn").click()
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.page.evaluate("document.getElementById('lzInventoryForm').requestSubmit()")
        expect(self.page.locator("#toast")).to_have_text("请至少选择一个仓库")
        self.assertEqual(self.page.evaluate("submitted"), [])
        self.page.locator("#lzWarehouseSearch").fill("菲律宾")
        self.page.locator("#lzSelectWarehousesBtn").click()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0].warehouse_names"), ["菲律宾PH01仓"])
        self.assertEqual(self.page.evaluate("warehouseLoads"), 1)
        self.assertEqual(self.errors, [])

    def test_failed_initial_directory_load_preserves_restored_nondefault_selection_on_retry(self):
        restored = {"ok": True, "id": "restored", "status": "success", "logs": [], "context": {"account_id": "target", "warehouse_names": ["菲律宾PH01仓"]}, "result": self.result()}
        self.open_page(load_failure=True, restored_task=restored)
        self.page.wait_for_function("HQYL.state.taskId === 'restored'")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.page.evaluate("warehouseLoadFailure=false")
        self.page.locator("#lzRefreshWarehousesBtn").click()
        expect(self.page.locator("#lzWarehouseStatus")).to_contain_text("已加载 4")
        expect(self.page.locator("#lzWarehouseChecklist input:checked")).to_have_count(1)
        expect(self.page.locator('#lzWarehouseChecklist input[value="菲律宾PH01仓"]')).to_be_checked()
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()
        self.assertEqual(self.page.evaluate("warehouseLoads"), 2)
        self.assertEqual(self.errors, [])

    def test_partial_failure_keeps_successful_file_and_lists_failed_warehouses(self):
        self.open_page()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        result = self.result()
        result.update(exports=result["exports"][:1], output_files=result["output_files"][:1], is_complete=False,
                      completion_message="已生成 1 个仓库文件，2 个仓库导出失败", failures=[{"warehouse_name": "泰国TH05仓", "error": "下载返回错误"}, {"warehouse_name": "泰国TH06仓", "error": "接口超时"}])
        self.page.evaluate("result => { latestTask={...latestTask,status:'failed',error:result.completion_message,result}; }", result)
        expect(self.page.locator("#taskBadge")).to_have_text("结果不完整")
        expect(self.page.locator("#lzExportFilesBody tr")).to_have_count(1)
        expect(self.page.locator("#lzFailureSummary")).to_contain_text("泰国TH05仓：下载返回错误")
        expect(self.page.locator("#lzFailureSummary")).to_contain_text("泰国TH06仓：接口超时")
        expect(self.page.locator("#lzFileStatus")).to_have_text("1 个文件，2 仓失败")
        self.page.locator("#lzOpenFileBtn").click()
        self.assertEqual(self.page.evaluate("opened"), result["output_files"])
        self.assertEqual(self.errors, [])

    def test_all_failed_shows_no_generated_file_and_old_combined_result_is_labeled_history(self):
        self.open_page()
        failed = {"output_file": "", "output_files": [], "exports": [], "failures": [{"warehouse_name": "菲律宾PH01仓", "error": "接口超时"}], "is_complete": False, "completion_message": "1 个仓库导出失败", "selected_warehouse_count": 1}
        self.page.evaluate("result => HQYL.state.page.applyResult(result)", failed)
        expect(self.page.locator("#lzFileStatus")).to_have_text("未生成")
        expect(self.page.locator("#lzPreviewPanel")).to_be_hidden()
        expect(self.page.locator("#lzExportFiles")).to_be_hidden()
        expect(self.page.locator("#lzOpenFileBtn")).to_be_disabled()
        expect(self.page.locator("#lzOpenOutputBtn")).to_be_disabled()
        expect(self.page.locator("#lzFailureSummary")).to_contain_text("菲律宾PH01仓：接口超时")
        legacy = self.result()
        for key in ("output_files", "exports", "failures", "is_complete", "selected_warehouse_count"):
            legacy.pop(key)
        self.page.evaluate("result => HQYL.state.page.applyResult(result)", legacy)
        expect(self.page.locator("#lzResultSummary")).to_contain_text("历史结果")
        expect(self.page.locator("#lzResultSummary")).to_contain_text("重新运行将按仓库独立导出")
        expect(self.page.locator("#lzExportFilesBody tr")).to_have_count(1)
        expect(self.page.locator("#lzPreviewWarehouse")).to_contain_text("历史合并文件")
        self.assertEqual(self.errors, [])

    def test_switch_reloads_only_the_current_accounts_warehouses_and_submits_that_account(self):
        self.open_page()
        self.page.evaluate("result => {HQYL.state.page.applyResult(result);HQYL.appendLog('账号A旧日志');}", self.result())
        self.page.evaluate("switchMabang('other')")
        expect(self.page.locator("#lzAccountNote")).to_contain_text("测试账号B")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(2)
        expect(self.page.locator("#lzResult")).to_be_hidden()
        expect(self.page.locator("#logBox")).to_have_text("")
        expect(self.page.locator('#lzWarehouseChecklist input[value="泰国TH06仓"]')).to_be_checked()
        expect(self.page.locator('#lzWarehouseChecklist input[value="泰国TH04仓"]')).to_have_count(0)
        self.page.locator('#lzWarehouseChecklist input[value="B独有仓"]').check()
        self.page.locator('#lzWarehouseChecklist input[value="泰国TH06仓"]').uncheck()
        loads = self.page.evaluate("warehouseLoads")
        self.page.evaluate("HQYL.applyAccountState({accounts:HQYL.state.accounts,active_account_ids:{mabang:'other',ziniao:'ziniao'}})")
        self.assertEqual(self.page.evaluate("warehouseLoads"), loads)
        expect(self.page.locator('#lzWarehouseChecklist input[value="B独有仓"]')).to_be_checked()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0].account_id"), "other")
        self.assertEqual(self.page.evaluate("submitted[0].warehouse_names"), ["B独有仓"])
        self.assertEqual(self.page.evaluate("catalogRequests.map(item=>item.account_id)"), ["target", "other"])
        self.assertEqual(self.errors, [])

    def test_slow_previous_account_info_cannot_select_or_load_its_warehouses_after_switch(self):
        self.open_page(defer_info_ids=["target"], wait_for_load=False)
        self.page.wait_for_function("pendingInfo.length === 1")
        self.page.evaluate("switchMabang('other')")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(2)
        self.page.evaluate("pendingInfo.shift().resolve()")
        expect(self.page.locator("#lzAccountNote")).to_contain_text("测试账号B")
        self.assertEqual(self.page.evaluate("catalogRequests.map(item=>item.account_id)"), ["other"])
        self.assertEqual(self.page.evaluate("selected"), [])
        self.assertEqual(self.errors, [])

    def test_slow_previous_catalog_is_ignored_even_when_switching_back_to_the_same_account(self):
        self.open_page(defer_catalog_ids=["target"], wait_for_load=False)
        self.page.wait_for_function("pendingCatalogs.length === 1")
        self.page.evaluate("switchMabang('other')")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(2)
        self.page.evaluate("switchMabang('target')")
        self.page.wait_for_function("pendingCatalogs.length === 2")
        self.page.evaluate("pendingCatalogs.shift().resolve()")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        expect(self.page.locator("#lzWarehouseStatus")).to_contain_text("正在读取")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(0)
        self.page.evaluate("pendingCatalogs.shift().resolve()")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(4)
        expect(self.page.locator("#lzWarehouseChecklist input:checked")).to_have_count(3)
        self.assertEqual(self.errors, [])

    def test_late_previous_start_response_cannot_replace_the_new_accounts_running_task(self):
        self.open_page()
        self.page.evaluate("deferStarts=true")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("pendingStarts.length === 1")
        self.page.evaluate("switchMabang('other');deferStarts=false")
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("HQYL.state.taskId === 'lz-task-2'")
        self.page.evaluate("pendingStarts.shift()()")
        expect(self.page.locator("#logBox")).to_contain_text("other 正在导出")
        self.assertNotIn("target", self.page.locator("#logBox").inner_text())
        self.assertEqual(self.page.evaluate("HQYL.state.taskId"), "lz-task-2")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_inflight_previous_poll_cannot_render_old_result_after_account_switch(self):
        self.open_page()
        self.page.evaluate("deferPolls=true")
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("pendingPolls.length >= 1")
        self.page.evaluate("switchMabang('other');deferPolls=false")
        expect(self.page.locator("#lzRunBtn")).to_be_enabled()
        old = {"ok": True, "id": "lz-task-1", "status": "success", "logs": ["账号A完成"], "context": {"account_id": "target"}, "result": self.result()}
        self.page.evaluate("task => pendingPolls.shift().resolve(task)", old)
        expect(self.page.locator("#lzResult")).to_be_hidden()
        expect(self.page.locator("#logBox")).to_have_text("")
        self.assertEqual(self.page.evaluate("HQYL.state.taskId"), "")
        self.assertEqual(self.page.evaluate("HQYL.state.pollTimer"), 0)
        self.assertEqual(self.errors, [])

    def test_no_account_can_be_bound_and_loads_the_newly_selected_accounts_catalog(self):
        self.open_page(missing_account=True)
        self.assertEqual(self.page.evaluate("infoRequests"), [])
        self.assertEqual(self.page.evaluate("catalogRequests"), [])
        self.page.locator("#lzBindAccountBtn").click()
        expect(self.page.locator("#quickAccountModal")).to_be_visible()
        self.page.locator("#quickAccountName").fill("新绑定测试账号")
        self.page.locator("#quickUsername").fill("fixture-new-user")
        self.page.locator("#quickPassword").fill("fixture-new-secret")
        self.page.locator("#submitQuickAccountBtn").click()
        expect(self.page.locator("#lzAccountNote")).to_contain_text("新绑定测试账号")
        expect(self.page.locator("#lzWarehouseChecklist input")).to_have_count(4)
        expect(self.page.locator("#lzBindAccountBtn")).to_be_hidden()
        self.page.locator("#lzRunBtn").click()
        self.page.wait_for_function("submitted.length === 1")
        self.assertEqual(self.page.evaluate("submitted[0].account_id"), "bound")
        self.assertEqual(self.errors, [])

    def test_historical_task_without_an_account_id_is_not_restored(self):
        restored = {"ok": True, "id": "unknown-account", "status": "success", "logs": ["旧任务日志"], "context": {}, "result": self.result()}
        self.open_page(restored_task=restored)
        expect(self.page.locator("#lzResult")).to_be_hidden()
        expect(self.page.locator("#logBox")).to_have_text("")
        self.assertEqual(self.page.evaluate("HQYL.state.taskId"), "")
        self.assertEqual(self.errors, [])

    def test_old_info_signature_shows_full_restart_hint_without_a_parameterless_retry(self):
        self.open_page(info_failure="AppBridge.get_lz_warehouse_inventory_info() takes 1 positional argument but 2 were given", wait_for_load=False)
        expect(self.page.locator("#lzAccountNote")).to_have_text("后台版本尚未更新，请关闭整个工作台后重新启动；刷新页面无法更新后台")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("infoRequests"), [{"account_id": "target"}])
        self.assertEqual(self.page.evaluate("catalogRequests"), [])
        self.assertEqual(self.page.evaluate("selected"), [])
        self.assertEqual(self.errors, [])

    def test_old_catalog_signature_shows_full_restart_hint_without_a_parameterless_retry(self):
        self.open_page(catalog_failure="AppBridge.get_lz_warehouse_inventory_warehouses() takes 1 positional argument but 2 were given", wait_for_load=False)
        expect(self.page.locator("#lzWarehouseStatus")).to_have_text("后台版本尚未更新，请关闭整个工作台后重新启动；刷新页面无法更新后台")
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("catalogRequests"), [{"account_id": "target"}])
        self.assertEqual(self.page.evaluate("selected"), [])
        self.assertEqual(self.errors, [])

    def test_backend_authentication_rejection_remains_visible_without_rewording(self):
        message = "登录失败，账号或密码不正确"
        self.open_page(authentication_failure=message, wait_for_load=False)
        expect(self.page.locator("#lzWarehouseStatus")).to_contain_text(message)
        expect(self.page.locator("#toast")).to_have_text(message)
        expect(self.page.locator("#lzRunBtn")).to_be_disabled()
        self.assertEqual(self.page.evaluate("catalogRequests"), [{"account_id": "target"}])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
