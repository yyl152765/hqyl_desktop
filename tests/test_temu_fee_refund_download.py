from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from backend.services import temu_fee_refund_download as download
from test_temu_balance_calendar import Error, sync_playwright


class BrowserFixture:
    def __init__(self, page):
        self.page = page
        self.clicks = 0

    def evaluate(self, store_id, script):
        return json.loads(self.page.evaluate(script))

    def click(self, store_id, selector):
        self.clicks += 1
        self.page.locator(selector).click()


class FeeRefundDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest('Playwright unavailable')
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel='msedge')
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)
        workbook = Workbook()
        workbook.active.append(['订单编号'])
        workbook.active.append(['00123456789012345678'])
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        cls.xlsx = stream.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.requests = []
        def route(request):
            self.requests.append(request.request.url)
            if '.xlsx' in request.request.url:
                request.fulfill(status=200, body=self.xlsx, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            else:
                request.fulfill(status=200, body='<button id="download">下载</button>', content_type='text/html')
        self.page.route('**/*', route)
        self.page.goto('https://agentseller.temu.com/fixture')
        self.page.evaluate('''() => {
          window.open=function(...args){window.originalOpenArgs=args;return null;};
          window.savedOpen=window.open;window.savedCreate=URL.createObjectURL;
        }''')
        self.transport = BrowserFixture(self.page)

    def assert_clean(self):
        self.assertTrue(self.page.evaluate('window.open===window.savedOpen && URL.createObjectURL===window.savedCreate'))
        self.assertEqual(self.page.evaluate('Object.keys(window).filter(k=>k.startsWith("__hqyl_fee_download_")).length'), 0)
        self.assertFalse(list(self.directory.glob('.*.xlsx')))

    def receive(self):
        return download.download_xlsx(self.transport, 'store', '#download', self.directory, timeout=2)

    def test_window_open_download_captures_new_bytes_and_restores_hooks(self):
        self.page.evaluate('''() => document.querySelector('#download').onclick=()=>window.open('/bill-seller-protal/test.xlsx?signature=private')''')
        path = self.receive()
        self.assertEqual(path.read_bytes(), self.xlsx)
        self.assertEqual(self.transport.clicks, 1)
        self.assertEqual(self.page.evaluate('window.originalOpenArgs[0]'), '/bill-seller-protal/test.xlsx?signature=private')
        workbook = load_workbook(path, read_only=True)
        try:
            self.assertEqual(workbook.active['A2'].value, '00123456789012345678')
        finally:
            workbook.close()
        self.assert_clean()

    def test_blob_and_anchor_duplicate_capture_is_one_file(self):
        self.page.evaluate('''bytes => document.querySelector('#download').onclick=()=>{
          const blob=new Blob([new Uint8Array(bytes)]);const a=document.createElement('a');
          a.href=URL.createObjectURL(blob);a.download='export.xlsx';document.body.append(a);a.click();a.remove();
        }''', list(self.xlsx))
        self.assertEqual(self.receive().read_bytes(), self.xlsx)
        self.assertEqual(self.transport.clicks, 1)
        self.assert_clean()

    def test_unrelated_origin_is_rejected_without_signed_url_in_error(self):
        self.page.evaluate('''() => document.querySelector('#download').onclick=()=>window.open('https://outside.example/report.xlsx?secret=value')''')
        with self.assertRaisesRegex(ValueError, '不属于当前卖家页面') as raised:
            self.receive()
        self.assertNotIn('secret', str(raised.exception))
        self.assertNotIn('outside.example', str(raised.exception))
        self.assertFalse(any('outside.example' in url for url in self.requests))
        self.assertEqual(self.transport.clicks, 1)
        self.assert_clean()

    def test_invalid_payload_never_publishes_file_and_click_is_not_retried(self):
        self.page.route('**/invalid.xlsx', lambda route: route.fulfill(status=200, body=b'<html>login</html>', content_type='text/html'))
        self.page.evaluate('''() => document.querySelector('#download').onclick=()=>window.open('/invalid.xlsx')''')
        with self.assertRaisesRegex(ValueError, '不是有效 Excel'):
            self.receive()
        self.assertFalse(list(self.directory.iterdir()))
        self.assertEqual(self.transport.clicks, 1)
        self.assert_clean()

    def test_multiple_new_files_rejected_and_existing_file_preserved(self):
        self.page.evaluate('''() => document.querySelector('#download').onclick=()=>{
          window.open('/one.xlsx');window.open('/two.xlsx');
        }''')
        with self.assertRaisesRegex(ValueError, '多个文件'):
            self.receive()
        self.assert_clean()
        (self.directory / '导出.xlsx').write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.receive()
        self.assertEqual((self.directory / '导出.xlsx').read_bytes(), b'existing')
        self.assertEqual(self.transport.clicks, 1)


if __name__ == '__main__':
    unittest.main()
