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

    def open_page(self, missing_account=False, restored_task=None, load_failure=False):
        self.page.goto("about:blank")
        html = (FRONTEND / "pages/lz-warehouse-realtime-inventory.html").read_text(encoding="utf-8")
        html = re.sub(r'<script src="[^"]+"></script>|<link[^>]+>', "", html)
        self.page.set_content(html)
        self.page.add_style_tag(path=str(FRONTEND / "assets/app.css"))
        self.page.evaluate("""({missingAccount, restoredTask, loadFailure}) => {
          window.submitted = []; window.selected = []; window.opened = []; window.revealed = [];
          window.startFailure = ''; window.latestTask = restoredTask || {ok:true, empty:true};
          window.warehouseLoadFailure = loadFailure; window.warehouseLoads = 0;
          window.warehouseOptions = ['泰国TH04仓','泰国TH05仓','泰国TH06仓','菲律宾PH01仓'].map((name,index)=>({id:`W-${index}`,name}));
          const accounts = [
            {id:'other',vendor:'mabang',name:'其他测试账号',username:'other-user'},
            {id:'target',vendor:'mabang',name:'指定测试账号',username:'fixture-lz-user'},
          ];
          let activeId = 'other';
          window.pywebview = {api: {
            async get_app_info() { return {ok:true, app:{name:'测试',version:'test'}, settings:{output_dir:'C:/Exports'},
              account_state:{accounts,active_account_ids:{mabang:activeId}}}; },
            async get_sidebar_preferences() { return {ok:true,preferences:{}}; },
            async get_lz_warehouse_inventory_info() { return missingAccount
              ? {ok:false,requires_account:true,vendor:'mabang',error:'请先绑定本功能指定的马帮账号'}
              : {ok:true,account_id:'target',account_name:'指定测试账号',output_dir:'C:/Exports',warehouses:['泰国TH04仓','泰国TH05仓','泰国TH06仓']}; },
            async get_lz_warehouse_inventory_warehouses() { warehouseLoads++; return warehouseLoadFailure ? {ok:false,error:'目录接口暂时不可用'} : {ok:true,warehouses:warehouseOptions}; },
            async select_account(vendor,id) { selected.push({vendor,id}); activeId=id; return {ok:true,account_state:{accounts,active_account_ids:{mabang:id}}}; },
            async get_latest_task_status(tool) { return tool === 'lz_warehouse_inventory' ? latestTask : {ok:true,empty:true}; },
            async start_lz_warehouse_inventory(payload) { submitted.push(payload); if(startFailure) return {ok:false,error:startFailure};
              latestTask={ok:true,id:'lz-task',tool:'lz_warehouse_inventory',status:'running',logs:['正在导出第 1 / 3 页'],context:payload}; return latestTask; },
            async get_task_status() { return latestTask; },
            async choose_output_dir() { return {ok:true,path:'C:/Chosen'}; },
            async choose_scoped_output_dir() { return {ok:true,path:'C:/Chosen'}; },
            async save_scoped_output_dirs() { return {ok:true}; },
            async open_path(path) { opened.push(path); return {ok:true}; },
            async reveal_path(path) { revealed.push(path); return {ok:true}; },
          }};
        }""", {"missingAccount": missing_account, "restoredTask": restored_task, "loadFailure": load_failure})
        self.page.add_script_tag(path=str(FRONTEND / "assets/common.js"))
        self.page.add_script_tag(path=str(FRONTEND / "assets/pages/lz-warehouse-realtime-inventory.js"))
        if missing_account:
            expect(self.page.locator("#lzAccountNote")).to_contain_text("请先绑定")
        else:
            expect(self.page.locator("#lzAccountNote")).to_contain_text("指定测试账号")
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

    def test_selects_designated_account_chooses_directory_and_runs_the_api(self):
        self.open_page()
        self.assertEqual(self.page.evaluate("selected"), [{"vendor": "mabang", "id": "target"}])
        expect(self.page.locator('[data-active-account-name][data-vendor="mabang"]')).to_contain_text("指定测试账号")
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


if __name__ == "__main__":
    unittest.main()
