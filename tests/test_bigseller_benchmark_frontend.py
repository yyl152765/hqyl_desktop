from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class BigSellerBenchmarkFrontendTests(unittest.TestCase):
    def test_incomplete_result_retry_and_resume_flow(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend behavior check")
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[1];
const html = fs.readFileSync(path.join(root, 'pages/bigseller-sku-benchmark.html'), 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], {
  value: '', textContent: '', hidden: false, disabled: false, style: {}, attrs: {}, handlers: {},
  options: [], scrollTop: 0, scrollHeight: 100, clientHeight: 100,
  classList: { remove() {}, add() {} },
  addEventListener(name, handler) { this.handlers[name] = handler; },
  setAttribute(name, value) { this.attrs[name] = value; }, focus() {},
  get innerHTML() { return this.markup || ''; },
  set innerHTML(value) {
    this.markup = value;
    this.options = [...value.matchAll(/<option value="([^"]*)"/g)].map((entry) => ({ value: entry[1] }));
    if (this.options.length) this.value = this.options[0].value;
  },
}]));
nodes.metric.value = 'views';
const calls = [], opened = [];
let latestTask, response, release;
const bridge = {
  async start_bigseller_sku_benchmark(payload) {
    calls.push(JSON.parse(JSON.stringify(payload)));
    const result = typeof response === 'function' ? await response(payload) : response;
    if (result.ok) latestTask = result;
    return result;
  },
  async inspect_bigseller_benchmark_file() { return { ok: true, sheets: ['Sheet1'], sheet_name: 'Sheet1' }; },
  async choose_bigseller_benchmark_checkpoint() { return { ok: true, path: 'C:/Exports/BigSeller对标进度/progress.json' }; },
  async open_path(file) { opened.push(file); return { ok: true }; },
  async get_task_status() { return latestTask; },
};
const window = { pywebview: { api: bridge }, setInterval() { return 1; }, clearInterval() {}, setTimeout() {} };
const context = vm.createContext({ document: { getElementById: (id) => nodes[id] }, window, URLSearchParams, location: { search: '?account=bigseller', hostname: '127.0.0.1' } });
const common = fs.readFileSync(path.join(root, 'assets/common.js'), 'utf8');
// Expose only the existing preview factory to exercise its production contract.
vm.runInContext(common.replace('    boot,', '    boot,\n    createPreviewApi,'), context);
const HQYL = vm.runInContext('HQYL', context);
let page;
HQYL.boot = (value) => { page = value; HQYL.state.page = value; };
HQYL.state.accounts = [{ id: 'account-a', vendor: 'bigseller' }];
HQYL.state.activeAccountIds = { bigseller: 'account-a' };
vm.runInContext(fs.readFileSync(path.join(root, 'assets/pages/bigseller-sku-benchmark.js'), 'utf8'), context);
const partial = {
  is_complete: false, completion_message: '结果不完整：仍有 1 个 SKU 查询失败',
  source_file: 'C:/Source/book.xlsx', sheet_name: 'Sheet1', metric: 'views', output_dir: 'C:/Exports',
  checkpoint_file: 'C:/Exports/BigSeller对标进度/progress.json', output_file: 'C:/Exports/partial.xlsx',
  sku_count: 4, confirmed_count: 3, matched_count: 2, not_found_count: 1, failed_count: 1,
  rows: [{ sku: 'A', status: 'matched' }, { sku: 'B', status: 'matched' }, { sku: 'C', status: 'not_found' }, { sku: 'D', status: 'failed' }],
};
let sequence = 0;
function task(result) {
  return { ok: true, id: `task-${++sequence}`, status: result.is_complete === false ? 'failed' : 'success', result, logs: [], context: {} };
}
async function renderResult(result) {
  latestTask = task(result);
  await HQYL.startTask(async () => latestTask);
  await Promise.resolve();
}
(async () => {
  await page.init({ settings: { output_dir: 'C:/Exports' } });
  await renderResult(partial);
  assert.equal(nodes.taskBadge.textContent, '结果不完整');
  assert.equal(nodes.statusText.textContent, partial.completion_message);
  assert.equal(nodes.queryResult.hidden, false);
  assert.equal(nodes.progressCount.textContent, '3 / 4');
  assert.equal(nodes.queryProgress.attrs['aria-valuenow'], '75');
  assert.equal(nodes.retryFailedBtn.hidden, false);
  assert.equal(nodes.openFileBtn.disabled, false);
  await nodes.openFileBtn.handlers.click();
  assert.equal(opened.at(-1), partial.output_file);

  page.onTaskUpdate({ status: 'running', logs: ['[BS对标进度 2/4]', '[10:20:30] [BS补查 第1轮] 等待 30 秒后补查'] });
  assert.equal(nodes.progressCount.textContent, '2 / 4');
  assert.match(nodes.progressText.textContent, /第1轮.*等待 30 秒/);
  page.onTaskUpdate({ status: 'running', logs: ['[BS补查 第1轮] 等待 30 秒', '[BS对标进度 3/4]'] });
  assert.equal(nodes.progressCount.textContent, '3 / 4');
  assert.ok(!nodes.progressText.textContent.includes('等待 30 秒'));
  page.applyResult({ ...partial, sku_count: 4, confirmed_count: 0, matched_count: 0, not_found_count: 0, failed_count: 4 });
  assert.equal(nodes.queryProgress.attrs['aria-valuenow'], '0');
  page.applyResult({ ...partial, sku_count: 1000, confirmed_count: 999 });
  assert.equal(nodes.queryProgress.attrs['aria-valuenow'], '99');

  await renderResult(partial);
  nodes.sourceFile.value = 'C:/Changed/other.xlsx';
  nodes.metric.value = 'sales';
  nodes.outputDir.value = 'C:/Changed';
  response = () => new Promise((resolve) => { release = () => resolve(task(partial)); });
  const pendingRetry = nodes.retryFailedBtn.handlers.click();
  assert.equal(nodes.retryFailedBtn.disabled, true);
  assert.equal(nodes.queryResult.hidden, false);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    source_file: partial.source_file, sheet_name: partial.sheet_name, metric: partial.metric,
    output_dir: partial.output_dir, resume_checkpoint: partial.checkpoint_file,
  });
  await nodes.retryFailedBtn.handlers.click();
  assert.equal(calls.length, 1);
  release();
  await pendingRetry;
  assert.equal(nodes.retryFailedBtn.disabled, false);
  assert.equal(nodes.resumeCheckpoint.value, '');

  response = { ok: false, error: '账号与进度文件不一致' };
  await nodes.retryFailedBtn.handlers.click();
  assert.equal(nodes.queryResult.hidden, false);
  assert.equal(nodes.retryFailedBtn.hidden, false);
  assert.equal(nodes.openFileBtn.disabled, false);
  assert.match(nodes.progressText.textContent, /补查未启动.*已保留/);
  assert.match(nodes.toast.textContent, /账号与进度文件不一致/);

  const complete = { ...partial, is_complete: true, completion_message: '全部 SKU 已确认', confirmed_count: 4, matched_count: 3, failed_count: 0, recovered_count: 1, resumed_count: 3 };
  response = task(complete);
  await nodes.retryFailedBtn.handlers.click();
  assert.equal(nodes.taskBadge.textContent, '已完成');
  assert.equal(nodes.progressCount.textContent, '4 / 4');
  assert.equal(nodes.retryFailedBtn.hidden, true);
  assert.equal(nodes.resumeCheckpoint.value, '');
  assert.match(nodes.resultSummary.textContent, /沿用已确认 3 个.*补查恢复 1 个/);

  await nodes.chooseCheckpointBtn.handlers.click();
  assert.equal(nodes.resumeCheckpoint.value, partial.checkpoint_file);
  nodes.sourceFile.handlers.input();
  assert.equal(nodes.resumeCheckpoint.value, '');
  await nodes.chooseCheckpointBtn.handlers.click();
  nodes.metric.handlers.change();
  assert.equal(nodes.resumeCheckpoint.value, '');
  await nodes.chooseCheckpointBtn.handlers.click();
  nodes.sheetName.handlers.change();
  assert.equal(nodes.resumeCheckpoint.value, '');
  await nodes.chooseCheckpointBtn.handlers.click();
  response = task(complete);
  await nodes.benchmarkForm.handlers.submit({ preventDefault() {} });
  assert.equal(calls.at(-1).resume_checkpoint, partial.checkpoint_file);
  response = task(complete);
  await nodes.benchmarkForm.handlers.submit({ preventDefault() {} });
  assert.ok(!Object.hasOwn(calls.at(-1), 'resume_checkpoint'));

  const exportFailed = { ...complete, is_complete: false, output_file: '', completion_message: '查询结果已保存，Excel 导出未完成', preview_limited: true };
  await renderResult(exportFailed);
  assert.equal(nodes.taskBadge.textContent, '结果不完整');
  assert.equal(nodes.progressCount.textContent, '4 / 4');
  assert.equal(nodes.queryProgress.attrs['aria-valuenow'], '100');
  assert.equal(nodes.retryFailedBtn.textContent, '重新导出已确认结果');
  assert.equal(nodes.openFileBtn.disabled, true);
  assert.match(nodes.failureNotice.textContent, /查询结果已保存，Excel 导出未完成/);
  assert.ok(!nodes.failureNotice.textContent.includes('仍有 0 个查询失败'));
  assert.ok(!nodes.failureNotice.textContent.includes('查看部分结果 Excel'));
  assert.ok(!nodes.resultSummary.textContent.includes('结果见 Excel'));
  assert.ok(!nodes.resultLimitNote.textContent.includes('结果见 Excel'));
  response = () => new Promise((resolve) => { release = () => resolve(task(complete)); });
  const pendingExport = nodes.retryFailedBtn.handlers.click();
  assert.equal(nodes.retryFailedBtn.textContent, '正在重新导出…');
  assert.equal(nodes.retryFailedBtn.disabled, true);
  assert.equal(calls.at(-1).resume_checkpoint, partial.checkpoint_file);
  assert.match(nodes.logBox.textContent, /重新导出已确认结果.*无需重新查询/);
  release();
  await pendingExport;
  assert.equal(nodes.taskBadge.textContent, '已完成');
  assert.equal(nodes.retryFailedBtn.hidden, true);
  assert.equal(nodes.openFileBtn.disabled, false);

  const previewApi = HQYL.createPreviewApi();
  const preview = await previewApi.start_bigseller_sku_benchmark({ source_file: 'demo.xlsx', sheet_name: 'Sheet1', metric: 'sales' });
  assert.equal(preview.status, 'failed');
  assert.equal(preview.result.is_complete, false);
  assert.equal(preview.result.confirmed_count, 3);
  assert.equal(preview.result.checkpoint_file, '');
  await renderResult(preview.result);
  assert.equal(nodes.retryFailedBtn.hidden, true);
  assert.equal(nodes.openFileBtn.disabled, true);
  assert.equal(nodes.openOutputBtn.disabled, true);
  assert.equal(nodes.progressCount.textContent, '3 / 4');
  assert.match(nodes.failureNotice.textContent, /演示模式不支持补查/);
  assert.equal((await previewApi.start_bigseller_sku_benchmark({ resume_checkpoint: 'fake.json' })).ok, false);

  let applied = 0;
  HQYL.state.page = { applyResult() { applied++; } };
  latestTask = { ok: true, id: 'ordinary-failed', status: 'failed', logs: [], error: '普通任务失败' };
  await HQYL.startTask(async () => latestTask);
  assert.equal(applied, 0);
  assert.equal(nodes.taskBadge.textContent, '失败');
  console.log('BigSeller completeness, recovery and failure rendering verified');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script, str(FRONTEND)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
