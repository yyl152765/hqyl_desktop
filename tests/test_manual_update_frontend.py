from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class ManualUpdateFrontendTests(unittest.TestCase):
    def test_updates_require_explicit_user_actions_and_never_block_settings(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the frontend behavior check")

        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[1];
const html = fs.readFileSync(path.join(root, 'pages/settings.html'), 'utf8');
function element() {
  return {
    value: '', textContent: '', hidden: false, disabled: false, style: {}, handlers: {},
    classList: { toggle() {} }, appendChild() {}, querySelector: () => ({ textContent: '' }),
    addEventListener(name, handler) { this.handlers[name] = handler; },
  };
}
const nodes = Object.fromEntries([...html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)]
  .map(match => [match[1], Object.assign(element(), { hidden: /\bhidden\b/.test(match[0]) })]));
const calls = { checks: 0, downloads: 0, installs: [], saved: [], accounts: [] };
const timers = new Map(), notices = [];
let page, nextTimer = 1, failCheck = false, downloadComplete = false;
const bridge = {
  async check_for_updates() {
    calls.checks++;
    if (failCheck) return { ok: false, configured: true, error: '更新服务器不可用' };
    return { ok: true, configured: true, current_version: '0.2.1', latest_version: '0.2.2',
      update_available: true, mandatory: true, notes: '测试新版本' };
  },
  async start_update_download() { calls.downloads++; return { ok: true, id: 'manual-download' }; },
  async get_update_task_status(id) {
    assert.equal(id, 'manual-download');
    return { ok: true, status: downloadComplete ? 'success' : 'running', logs: ['下载进度 50%'],
      result: { installer_path: 'C:/Updates/setup.exe' } };
  },
  async install_downloaded_update(installer) { calls.installs.push(installer); return { ok: true }; },
  async save_settings(payload) { calls.saved.push(payload); return { ok: true, settings: payload }; },
};
const HQYL = {
  $: id => nodes[id], api: () => bridge, boot: value => { page = value; }, state: {},
  vendorAccounts: () => [], activeAccount: () => null,
  showToast: message => notices.push(message), openAccountDialog: vendor => calls.accounts.push(vendor),
};
const window = {
  clearInterval(id) { timers.delete(id); },
  setInterval(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
  setTimeout() { throw new Error('Unexpected startup timer'); },
  confirm() { throw new Error('Unexpected blocking confirmation'); },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/pages/settings.js'), 'utf8'),
  { HQYL, window, document: { createElement: element }, location: { search: '' }, URLSearchParams });

(async () => {
  await page.init({ settings: { output_dir: 'C:/Exports', update_manifest_url: 'https://example.com/latest.json' } });
  assert.equal(calls.checks, 0, 'opening settings must not contact the update server');
  assert.equal(calls.downloads, 0);
  assert.equal(calls.installs.length, 0);
  assert.equal(timers.size, 0, 'opening settings must not schedule automatic updates');
  assert.equal(nodes.updateActionPanel.hidden, true);

  await nodes.checkUpdateBtn.handlers.click();
  assert.equal(calls.checks, 1);
  assert.equal(calls.downloads, 0, 'even mandatory:true is only an available update');
  assert.equal(calls.installs.length, 0);
  assert.equal(nodes.updateActionPanel.hidden, false);
  assert.equal(nodes.availableUpdateVersion.textContent, '0.2.2');
  assert.equal(timers.size, 0);
  assert.equal(nodes.saveSettingsBtn.disabled, false);
  nodes.outputDir.value = 'C:/ContinueUsingOldVersion';
  await nodes.saveSettingsBtn.handlers.click();
  assert.equal(HQYL.state.outputDir, 'C:/ContinueUsingOldVersion');
  assert.equal(notices.at(-1), '配置已保存');
  await nodes.addMabangAccountBtn.handlers.click();
  assert.deepEqual(calls.accounts, ['mabang']);
  assert.equal(calls.downloads, 0, 'ordinary actions must remain independent of updates');

  failCheck = true;
  await nodes.checkUpdateBtn.handlers.click();
  assert.equal(nodes.updateStatus.textContent, '更新服务器不可用');
  assert.equal(nodes.updateActionPanel.hidden, true);
  assert.equal(nodes.saveSettingsBtn.disabled, false);
  nodes.outputDir.value = 'C:/AfterFailedCheck';
  await nodes.saveSettingsBtn.handlers.click();
  assert.equal(HQYL.state.outputDir, 'C:/AfterFailedCheck');
  await nodes.downloadUpdateBtn.handlers.click();
  assert.equal(calls.downloads, 0, 'failed check must clear the previous update action');
  assert.equal(calls.installs.length, 0);

  failCheck = false;
  await nodes.checkUpdateBtn.handlers.click();
  await nodes.downloadUpdateBtn.handlers.click();
  assert.equal(calls.downloads, 1, 'explicit download button starts the update');
  assert.equal(calls.installs.length, 0, 'installation waits for a completed download');
  assert.equal(timers.size, 1);
  downloadComplete = true;
  await [...timers.values()][0]();
  assert.deepEqual(calls.installs, ['C:/Updates/setup.exe']);
  assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script, str(FRONTEND)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
