from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class TemuShippingFrontendTests(unittest.TestCase):
    def test_ordered_preview_resume_and_account_isolation(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for frontend behavior checks")
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.argv[1];
function element() {
  return {
    value: '', hidden: false, disabled: false, style: {}, attrs: {}, handlers: {}, children: [], text: '',
    scrollTop: 0, scrollHeight: 100, clientHeight: 100,
    classList: { remove() {}, add() {} },
    addEventListener(name, handler) { this.handlers[name] = handler; },
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; this.text = ''; },
    get textContent() { return this.text + this.children.map((child) => child.textContent).join(''); },
    set textContent(value) { this.text = String(value); this.children = []; },
    set innerHTML(value) { throw new Error('Untrusted table data must be rendered through textContent'); },
  };
}
const html = fs.readFileSync(path.join(root, 'pages/temu-shipping-channel.html'), 'utf8');
const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], element()]));
const inputRows = Array.from({length: 80}, (_, index) => ({ excel_row: index + 2, length: index + 10, width: 15, height: 11, weight: 440 }));
const imported = { ok: true, file_path: 'C:/Source/daily.xlsx', workbook: {
  input_file: 'C:/Source/daily.xlsx', sheet_name: 'Sheet1', row_count: 80, rows: inputRows, warnings: ['已固定今日数据'],
} };
const preview = { mode: 'preview', run_id: 'run-a', selection_policy: 'enabled_only', input_file: imported.file_path, sheet_name: 'Sheet1',
  excel_row_count: 80, channel_count: 80, ready_count: 80, success_count: 0, failed_count: 0, complete: false, can_apply: true,
  rows: inputRows.map((row, index) => ({ ...row, channel_id: `channel-${index}`, channel_name: `TEMU ${index}`, status: 'ready' })),
};
preview.rows[0].channel_name = '<img src=x onerror=bad()> TEMU';
preview.rows[0].message = '<script>bad()</script>';
const partial = { ...preview, mode: 'batch', is_complete: false, success_count: 79, failed_count: 1,
  completion_message: '有 1 个渠道未完成', rows: preview.rows.map((row, index) => ({ ...row, status: index === 79 ? 'failed' : 'success' })),
};
const complete = { ...partial, complete: true, is_complete: true, can_apply: false, success_count: 80, failed_count: 0,
  rows: partial.rows.map((row) => ({ ...row, status: 'success' })),
};
let active = 'account-a', sequence = 0, latestTask, batchResponse, release, checkpointReader, inspectResponse;
const calls = [], saved = {}, tasks = {};
function task(result, status, owner = active) {
  return { ok: true, id: `task-${++sequence}`, tool: result.mode === 'preview' ? 'temu_shipping_preview' : 'temu_shipping_batch',
    status: status || (result.is_complete === false ? 'failed' : 'success'), result, logs: [],
    context: { account_id: owner, input_file: result.input_file, run_id: result.run_id }, created_at: '2026-09-10 10:00:00',
  };
}
const bridge = {
  async get_temu_shipping_checkpoint(payload) { return checkpointReader ? checkpointReader(payload) : { ok: true, result: saved[payload.account_id] || null }; },
  async choose_temu_shipping_file() { return imported; },
  async inspect_temu_shipping_file(payload) { calls.push(['inspect', payload]); return inspectResponse ? inspectResponse(payload) : imported; },
  async start_temu_shipping_preview(payload) { calls.push(['preview', payload]); latestTask = task(preview); return latestTask; },
  async start_temu_shipping_batch(payload) { calls.push(['batch', payload]); latestTask = typeof batchResponse === 'function' ? await batchResponse(payload) : batchResponse; return latestTask; },
  async get_task_status() { return latestTask; },
  async get_latest_task_status(tool) { return tasks[tool] || { ok: false, empty: true }; },
};
let poll;
const window = { pywebview: { api: bridge }, setInterval(callback) { poll = callback; return 1; }, clearInterval() {}, setTimeout() {} };
const context = vm.createContext({ document: { getElementById: (id) => nodes[id], createElement: element }, window, URLSearchParams,
  location: { hostname: '127.0.0.1', search: '' } });
vm.runInContext(fs.readFileSync(path.join(root, 'assets/common.js'), 'utf8'), context);
const HQYL = vm.runInContext('HQYL', context);
let page;
HQYL.boot = (value) => { page = value; HQYL.state.page = value; };
HQYL.state.accounts = ['account-a', 'account-b'].map((id) => ({ id, vendor: 'mabang' }));
HQYL.state.activeAccountIds = { mabang: active };
vm.runInContext(fs.readFileSync(path.join(root, 'assets/pages/temu-shipping-channel.js'), 'utf8'), context);
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
async function show(result, status) { latestTask = task(result, status); await HQYL.startTask(async () => latestTask); await tick(); }
async function switchAccount(owner) { active = owner; HQYL.state.activeAccountIds.mabang = owner; await page.onAccountsChanged(); }
(async () => {
  await page.init();
  await nodes.chooseFileBtn.handlers.click();
  assert.equal(nodes.excelRowCount.textContent, '80');
  assert.equal(nodes.resultTableBody.children.length, 50);
  assert.equal(nodes.applyBtn.disabled, true);
  assert.match(nodes.fileWarning.textContent, /今日/);
  await nodes.shippingForm.handlers.submit({ preventDefault() {} });
  assert.equal(calls.at(-1)[0], 'preview');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1])), { account_id: 'account-a', input_file: imported.file_path });
  assert.equal(nodes.applyBtn.disabled, false);
  assert.match(nodes.resultSummary.textContent, /80 个已开启渠道/);
  assert.match(nodes.resultSummary.textContent, /已关闭渠道不占用 Excel 行/);
  assert.equal(nodes.resultTableBody.children[0].children[2].textContent, '2');
  assert.equal(nodes.resultTableBody.children[0].children[3].textContent, '10目标值 · 执行时读取原值');
  assert.ok(nodes.resultTableBody.children[0].children[1].textContent.includes('<img src=x onerror=bad()>'));
  nodes.resultNextBtn.handlers.click();
  assert.equal(nodes.resultTableBody.children.length, 30);
  assert.equal(nodes.resultTableBody.children[0].children[2].textContent, '52');
  assert.equal(nodes.resultTableBody.children[0].children[3].textContent, '60目标值 · 执行时读取原值');
  assert.equal(nodes.resultNextBtn.disabled, true);

  await show({ ...preview, channel_count: 81, can_apply: false, message: 'Excel 数据不足' });
  assert.equal(nodes.applyBtn.disabled, true);
  assert.match(nodes.resultNotice.textContent, /不足/);
  const before = calls.length;
  await nodes.applyBtn.handlers.click();
  assert.equal(calls.length, before);
  await show(preview);
  batchResponse = () => new Promise((resolve) => { release = () => resolve(task(partial)); });
  const pending = nodes.applyBtn.handlers.click();
  assert.equal(nodes.inputFile.disabled, true);
  assert.equal(nodes.chooseFileBtn.disabled, true);
  assert.equal(nodes.applyBtn.disabled, true);
  await nodes.applyBtn.handlers.click();
  assert.equal(calls.filter(([name]) => name === 'batch').length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1])), { account_id: 'account-a', run_id: 'run-a' });
  release(); await pending; await tick();
  assert.equal(nodes.applyBtn.textContent, '继续未完成渠道');
  assert.equal(nodes.applyBtn.disabled, false);
  assert.equal(nodes.progressCount.textContent, '79 / 80');
  assert.equal(nodes.taskBadge.textContent, '结果不完整');
  batchResponse = { ok: false, error: '登录已过期' };
  await nodes.applyBtn.handlers.click();
  assert.equal(nodes.applyBtn.disabled, false);
  assert.match(nodes.resultNotice.textContent, /数据已保留/);
  batchResponse = task(complete);
  await nodes.applyBtn.handlers.click(); await tick();
  assert.equal(calls.at(-1)[1].run_id, 'run-a');
  assert.equal(nodes.progressCount.textContent, '80 / 80');
  assert.equal(nodes.applyBtn.disabled, true);

  // Original, target and actual readback are distinct; failed values cannot look saved.
  const changes = { ...partial, channel_count: 3, changed_count: 1, unchanged_count: 1, success_count: 2, failed_count: 1,
    rows: [
      { ...preview.rows[0], length: '16', width: '12', height: '10', weight: '300', status: 'success',
        original_values: { length: '15', width: '10', height: '11', weight: '275' },
        observed_values: { length: '16', width: '12', height: '10', weight: '300' }, observation_stage: 'after_save' },
      { ...preview.rows[1], length: '15', weight: '275', status: 'unchanged',
        original_values: { length: '15', weight: '275' }, observed_values: { length: '15', weight: '275' }, observation_stage: 'before_save' },
      { ...preview.rows[2], length: '16', weight: '300', status: 'failed',
        original_values: { length: '15', weight: '275' }, observed_values: { length: '15', weight: '275' }, observation_stage: 'after_save' },
    ] };
  await show(changes);
  assert.equal(nodes.changeCounts.textContent, '已修改 1 · 无需修改 1');
  assert.equal(nodes.resultTableBody.children[0].children[3].textContent, '15 → 16保存后：16');
  assert.equal(nodes.resultTableBody.children[0].children[6].textContent, '275 → 300保存后：300');
  assert.equal(nodes.resultTableBody.children[1].children[7].textContent, '无需修改');
  assert.match(nodes.resultTableBody.children[2].children[6].textContent, /275 → 300保存后：275未达目标/);
  assert.match(nodes.resultTableBody.children[2].children[6].children[0].className, /mismatch/);
  const noChanges = { ...changes, complete: true, is_complete: true, can_apply: false, changed_count: 0, unchanged_count: 3,
    success_count: 3, failed_count: 0, rows: changes.rows.map((row) => ({ ...row, status: 'unchanged' })),
    message: '本次未提交任何修改，当前值与本次导入数据相同' };
  await show(noChanges);
  assert.match(nodes.resultSummary.textContent, /已修改 0 个，无需修改 3 个/);
  assert.match(nodes.resultNotice.className, /warning/);
  assert.match(nodes.resultNotice.textContent, /未提交任何修改/);

  nodes.inputFile.value = 'C:/Source/changed.xlsx';
  nodes.inputFile.handlers.input();
  assert.equal(nodes.applyBtn.disabled, true);
  assert.equal(nodes.channelCount.textContent, '0');
  assert.equal(nodes.excelRowCount.textContent, '0');

  saved['account-b'] = { ...partial, run_id: 'run-b', input_file: 'C:/Source/b.xlsx' };
  let releaseOldStart;
  const oldStart = HQYL.startTask(() => new Promise((resolve) => { releaseOldStart = resolve; }));
  await switchAccount('account-b');
  assert.equal(nodes.inputFile.value, 'C:/Source/b.xlsx');
  assert.equal(nodes.applyBtn.textContent, '继续未完成渠道');
  latestTask = task(complete, 'success', 'account-a');
  // A response started under the old account cannot overwrite the active account.
  releaseOldStart(latestTask); await oldStart; await tick();
  assert.equal(nodes.inputFile.value, 'C:/Source/b.xlsx');
  assert.equal(nodes.channelCount.textContent, '80');
  assert.equal(nodes.successCount.textContent, '79');
  assert.equal(nodes.applyBtn.disabled, false);

  // Running batches refresh durable per-channel progress without restarting work.
  latestTask = { ...task(saved['account-b'], 'running'), result: null };
  await HQYL.startTask(async () => latestTask); await tick();
  assert.equal(nodes.successCount.textContent, '79');
  assert.equal(nodes.taskBadge.textContent, '运行中');
  assert.equal(nodes.applyBtn.disabled, true);
  await show(saved['account-b']);

  let releaseRestore;
  checkpointReader = ({account_id}) => account_id === 'account-a'
    ? new Promise((resolve) => { releaseRestore = () => resolve({ok: true, result: complete}); })
    : Promise.resolve({ok: true, result: saved['account-b']});
  const oldRestore = switchAccount('account-a');
  await switchAccount('account-b');
  releaseRestore(); await oldRestore;
  assert.equal(nodes.inputFile.value, 'C:/Source/b.xlsx');
  assert.equal(nodes.successCount.textContent, '79');
  checkpointReader = null;

  // An in-flight poll from a previously selected task must not overwrite a newer task.
  let releasePoll;
  bridge.get_task_status = () => new Promise((resolve) => { releasePoll = resolve; });
  latestTask = { ...task(preview, 'running'), result: null };
  await HQYL.startTask(async () => latestTask);
  const oldPollResponse = releasePoll;
  bridge.get_task_status = async () => latestTask;
  latestTask = task({...complete, run_id: 'run-b', input_file: 'C:/Source/b.xlsx'});
  await HQYL.startTask(async () => latestTask); await tick();
  oldPollResponse(task({...partial, input_file: 'C:/Source/old.xlsx'})); await tick();
  assert.equal(nodes.inputFile.value, 'C:/Source/b.xlsx');
  assert.equal(nodes.successCount.textContent, '80');

  // Late file inspection after switching account must not restore the old source.
  let releaseInspection;
  inspectResponse = () => new Promise((resolve) => { releaseInspection = resolve; });
  const oldInspection = nodes.inspectFileBtn.handlers.click();
  await switchAccount('account-a');
  releaseInspection(imported); await oldInspection;
  assert.equal(nodes.excelRowCount.textContent, '0');
  assert.equal(nodes.applyBtn.disabled, true);

  // Invalidated older mappings must keep the re-preview explanation visible.
  saved['account-b'] = { ...partial, can_apply: false, message: '旧记录包含关闭渠道，请重新查询预览' };
  await switchAccount('account-b');
  assert.equal(nodes.applyBtn.disabled, true);
  assert.equal(nodes.resultNotice.textContent, saved['account-b'].message);

  // Legacy checkpoint restoration and task results cannot restore the old 99-channel mapping.
  const legacy = { ...partial, run_id: 'legacy-run', channel_count: 99, can_apply: true };
  delete legacy.selection_policy;
  saved['account-a'] = legacy;
  await switchAccount('account-a');
  assert.equal(nodes.channelCount.textContent, '0');
  assert.equal(nodes.applyBtn.disabled, true);
  assert.equal(nodes.previewBtn.disabled, false);
  assert.match(nodes.resultNotice.textContent, /仅处理已开启.*重新查询预览/);
  assert.equal(nodes.resultTableBody.children.length, 1);
  const beforeLegacyApply = calls.length;
  await nodes.applyBtn.handlers.click();
  assert.equal(calls.length, beforeLegacyApply);

  saved['account-b'] = preview;
  tasks.temu_shipping_batch = task(legacy, 'failed', 'account-b');
  await switchAccount('account-b');
  assert.equal(nodes.channelCount.textContent, '0');
  assert.equal(nodes.applyBtn.disabled, true);
  assert.equal(nodes.previewBtn.disabled, false);
  assert.match(nodes.resultNotice.textContent, /仅处理已开启.*重新查询预览/);
  await show({ ...legacy, mode: 'preview' }, 'success');
  assert.equal(nodes.channelCount.textContent, '0');
  assert.equal(nodes.progressCount.textContent, '0 / 0');
  assert.equal(nodes.applyBtn.disabled, true);
  assert.match(nodes.resultNotice.textContent, /仅处理已开启.*重新查询预览/);

  // Direct running-task updates with an old result must not reintroduce its count.
  page.onTaskUpdate({ ...task(legacy, 'running'), progress: { total: 99, success_count: 12 } });
  assert.equal(nodes.progressCount.textContent, '0 / 0');
  assert.equal(nodes.channelCount.textContent, '0');
  await show(preview);
  assert.equal(nodes.channelCount.textContent, '80');
  assert.equal(nodes.applyBtn.disabled, false);
  console.log('TEMU frontend preview, resume, pagination and race checks passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script, str(FRONTEND)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
