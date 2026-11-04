from __future__ import annotations

import tempfile
import unittest
import base64
from io import BytesIO
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from PIL import Image, ImageDraw

from backend.services import balance_evidence
from backend.services.balance_evidence import BalanceEvidenceError, BalanceLayoutChangedError, capture_balance_evidence, capture_balance_page_evidence


@dataclass(frozen=True)
class Identity:
    hwnd: int = 100
    pid: int = 200
    title: str = "完整店铺名称"
    process_name: str = "ziniaobrowser.exe"
    process_created_at: int = 300
    visible: bool = True
    top_level: bool = True


def rendered_window(width=800, height=600):
    """Generated fixture only; never alters a captured screenshot."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 110), fill="#223344")
    draw.rectangle((40, 220, width - 40, height - 80), fill="#347ac2")
    draw.rectangle((80, 250, width - 80, height - 110), fill="#dddddd")
    return image


class BalanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "proof.png"
        self.identity = Identity()
        self.api = Mock()
        self.api.find_windows.return_value = [self.identity]
        self.api.placement.return_value = SimpleNamespace(show_cmd=1)
        self.api.rect.return_value = SimpleNamespace(width=800, height=600)
        self.api.identity.return_value = self.identity
        self.driver = SimpleNamespace(current_url="https://agentseller.temu.com/funds", execute_script=Mock(return_value=True))
        self.capture = Mock(side_effect=lambda *args: rendered_window())

    def capture_evidence(self, **options):
        return capture_balance_evidence(self.driver, self.path, self.identity.title, "账户总金额", window_api=self.api, capture_window=self.capture, **options)

    def test_exact_title_and_process_full_image_is_preserved(self):
        original = rendered_window()
        expected_pixels = original.tobytes()
        self.capture.side_effect = None
        self.capture.return_value = original
        self.api.find_windows.return_value = [
            replace(self.identity, hwnd=101, title="完整店铺名称-其他"),
            replace(self.identity, hwnd=102, process_name="chrome.exe"),
            replace(self.identity, hwnd=103, visible=False),
            replace(self.identity, hwnd=104, top_level=False),
            replace(self.identity, process_name="ZiNiaoBrowser.EXE"),
        ]
        self.api.identity.return_value = self.api.find_windows.return_value[-1]
        output = self.capture_evidence()
        self.assertEqual(output, str(self.path.resolve()))
        self.api.find_windows.assert_called_once_with("完整店铺名称")
        self.capture.assert_called_once_with(100, 800, 600)
        with Image.open(output) as saved:
            self.assertEqual(saved.size, (800, 600))
            self.assertEqual(saved.tobytes(), expected_pixels)
        with self.assertRaises(ValueError):
            original.getpixel((0, 0))  # Image resources are released.

    def test_absent_or_duplicate_exact_window_never_captures(self):
        for windows in ([], [self.identity, replace(self.identity, hwnd=101)], [replace(self.identity, title="完整店铺名称 其他")], [replace(self.identity, process_name="chrome.exe")]):
            with self.subTest(windows=windows):
                self.api.find_windows.return_value = windows
                with self.assertRaisesRegex(BalanceEvidenceError, "唯一确认"):
                    self.capture_evidence()
        self.capture.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_minimized_window_is_not_restored_or_captured(self):
        self.api.placement.return_value = SimpleNamespace(show_cmd=2)
        with self.assertRaisesRegex(BalanceEvidenceError, "最小化"):
            self.capture_evidence()
        self.capture.assert_not_called()
        self.api.position_no_activate.assert_not_called()
        self.api.restore_placement_no_activate.assert_not_called()

    def test_invalid_geometry_does_not_allocate_capture(self):
        for width, height in [(399, 600), (800, 249), (12001, 600), (800, 8001)]:
            with self.subTest(size=(width, height)):
                self.api.rect.return_value = SimpleNamespace(width=width, height=height)
                with self.assertRaisesRegex(BalanceEvidenceError, "尺寸"):
                    self.capture_evidence()
        self.capture.assert_not_called()

    def test_blank_or_wrong_sized_render_rejected(self):
        self.capture.side_effect = None
        for image in (Image.new("RGB", (800, 600), "black"), Image.new("RGB", (800, 600), "white"), rendered_window(801, 600)):
            with self.subTest(size=image.size):
                self.capture.return_value = image
                with self.assertRaisesRegex(BalanceEvidenceError, "为空白"):
                    self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_colorful_browser_frame_with_blank_page_is_rejected(self):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 800, 100), fill="blue")
        draw.rectangle((10, 20, 100, 80), fill="red")
        self.capture.side_effect = None
        self.capture.return_value = image
        with self.assertRaisesRegex(BalanceEvidenceError, "页面截图为空白"):
            self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_changed_window_identity_title_pid_or_process_creation_rejected(self):
        for new_identity in (replace(self.identity, title="另一店铺"), replace(self.identity, pid=201), replace(self.identity, process_created_at=301), replace(self.identity, visible=False)):
            with self.subTest(identity=new_identity):
                self.api.identity.return_value = new_identity
                with self.assertRaisesRegex(BalanceEvidenceError, "发生变化"):
                    self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_page_change_during_capture_rejected(self):
        class Driver:
            pass
        self.driver = Driver()
        self.driver.execute_script = Mock(return_value=True)
        with patch.object(Driver, "current_url", new_callable=PropertyMock, create=True, side_effect=["https://agentseller.temu.com/funds", "https://agentseller.temu.com/login"]):
            with self.assertRaisesRegex(BalanceEvidenceError, "发生变化"):
                self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_hidden_target_is_rejected_before_capture(self):
        self.driver.execute_script.return_value = False
        with self.assertRaisesRegex(BalanceEvidenceError, "不是当前可见页签"):
            self.capture_evidence()
        self.capture.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_target_becoming_hidden_after_capture_is_rejected(self):
        self.driver.execute_script.side_effect = [True, False]
        with self.assertRaisesRegex(BalanceEvidenceError, "资金页签已隐藏"):
            self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_visibility_requires_actual_boolean_true(self):
        self.driver.execute_script.return_value = "visible"
        with self.assertRaisesRegex(BalanceEvidenceError, "不是当前可见页签"):
            self.capture_evidence()
        self.capture.assert_not_called()

    def test_resize_or_minimize_during_capture_rejected(self):
        self.api.rect.side_effect = [SimpleNamespace(width=800, height=600), SimpleNamespace(width=1000, height=600)]
        with self.assertRaisesRegex(BalanceEvidenceError, "尺寸、位置或显示状态"):
            self.capture_evidence()
        self.assertFalse(self.path.exists())
        self.api.rect.side_effect = None
        self.api.placement.side_effect = [SimpleNamespace(show_cmd=1), SimpleNamespace(show_cmd=2)]
        with self.assertRaisesRegex(BalanceEvidenceError, "尺寸、位置或显示状态"):
            self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_same_size_window_moved_during_capture_rejected(self):
        self.api.rect.side_effect = [
            SimpleNamespace(width=800, height=600, left=0, top=0),
            SimpleNamespace(width=800, height=600, left=100, top=0),
        ]
        with self.assertRaisesRegex(BalanceEvidenceError, "尺寸、位置或显示状态"):
            self.capture_evidence()
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_keeps_prior_image_and_removes_temporary(self):
        self.path.write_bytes(b"prior evidence")
        with patch("backend.services.balance_evidence.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.capture_evidence()
        self.assertEqual(self.path.read_bytes(), b"prior evidence")
        self.assertEqual(list(self.path.parent.glob(".*.png")), [])

    def test_save_failure_does_not_leave_incomplete_image(self):
        with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.capture_evidence()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".*.png")), [])

    def test_missing_store_or_field_does_not_enumerate_windows(self):
        for name, field in (("", "income"), ("店铺", "")):
            with self.subTest(name=name, field=field), self.assertRaisesRegex(BalanceEvidenceError, "缺少"):
                capture_balance_evidence(self.driver, self.path, name, field, window_api=self.api, capture_window=self.capture)
        self.api.find_windows.assert_not_called()


class BalancePageEvidenceTests(unittest.TestCase):
    def setUp(self):
        BalanceEvidenceTests.setUp(self)
        self.clock = SimpleNamespace(now=0.0)
        self.clock.monotonic = lambda: self.clock.now
        self.clock.sleep = lambda seconds: setattr(self.clock, 'now', self.clock.now + seconds)
        clock_patch = patch.object(balance_evidence, 'time', self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.driver.current_url = 'https://sellercenter.lazada.co.th/apps/balance'
        self.driver.current_window_handle = 'balance-tab'
        with rendered_window(800, 1800) as page_image:
            self.expected_pixels = page_image.tobytes()
            buffer = BytesIO()
            page_image.save(buffer, 'PNG')
            self.payload = base64.b64encode(buffer.getvalue()).decode('ascii')
        self.driver.execute_cdp_cmd = Mock(side_effect=self.cdp)

    def cdp(self, method, params):
        if method == 'Page.getLayoutMetrics':
            return {'cssContentSize': {'width': 800, 'height': 1800}}
        self.assertEqual(method, 'Page.captureScreenshot')
        self.assertTrue(params['captureBeyondViewport'])
        self.assertEqual(params['clip'], {'x': 0, 'y': 0, 'width': 800, 'height': 1800, 'scale': 1})
        return {'data': self.payload}

    def capture_page(self):
        return capture_balance_page_evidence(self.driver, self.path, self.identity.title, 'Balance',
                                             window_api=self.api, capture_window=self.capture)

    def test_full_page_pixels_are_saved_with_separate_native_companion(self):
        output = self.capture_page()
        with Image.open(output) as saved:
            self.assertEqual(saved.size, (800, 1800))
            self.assertEqual(saved.tobytes(), self.expected_pixels)
        with Image.open(self.path.with_name('proof_window.png')) as companion:
            self.assertEqual(companion.size, (800, 600))

    def test_changed_store_identity_or_page_after_full_capture_rejects_output(self):
        for change in ('identity', 'url', 'handle'):
            with self.subTest(change=change):
                self.api.identity.return_value = self.identity
                self.driver.current_url = 'https://sellercenter.lazada.co.th/apps/balance'
                self.driver.current_window_handle = 'balance-tab'

                def cdp(method, params):
                    output = self.cdp(method, params)
                    if method == 'Page.captureScreenshot':
                        if change == 'identity':
                            self.api.identity.return_value = replace(self.identity, pid=201)
                        elif change == 'url':
                            self.driver.current_url = 'https://sellercenter.lazada.co.th/apps/login'
                        else:
                            self.driver.current_window_handle = 'other-tab'
                    return output

                self.driver.execute_cdp_cmd.side_effect = cdp
                with self.assertRaisesRegex(BalanceEvidenceError, '发生变化'):
                    self.capture_page()
                self.assertFalse(self.path.exists())

    def test_viewport_only_payload_is_rejected(self):
        with rendered_window(800, 600) as image:
            buffer = BytesIO()
            image.save(buffer, 'PNG')
            self.payload = base64.b64encode(buffer.getvalue()).decode('ascii')
        with self.assertRaisesRegex(BalanceEvidenceError, '被裁剪'):
            self.capture_page()
        self.assertFalse(self.path.exists())

    def test_blank_full_page_is_rejected(self):
        with Image.new('RGB', (800, 1800), 'white') as image:
            buffer = BytesIO()
            image.save(buffer, 'PNG')
            self.payload = base64.b64encode(buffer.getvalue()).decode('ascii')
        with self.assertRaisesRegex(BalanceEvidenceError, '为空白'):
            self.capture_page()
        self.assertFalse(self.path.exists())

    def test_waits_for_three_stable_measurements_before_capturing(self):
        heights = iter((1700, 1800, 1800, 1800, 1800, 1800))
        def cdp(method, params):
            if method == 'Page.getLayoutMetrics':
                return {'cssContentSize': {'width': 800, 'height': next(heights)}}
            return self.cdp(method, params)
        self.driver.execute_cdp_cmd.side_effect = cdp
        self.capture_page()
        self.assertEqual(sum(call.args[0] == 'Page.captureScreenshot' for call in self.driver.execute_cdp_cmd.call_args_list), 1)
        self.assertGreaterEqual(self.clock.now, .6)

    def test_continuous_resize_times_out_without_capturing(self):
        heights = iter(range(1800, 1900))
        def cdp(method, params):
            self.assertEqual(method, 'Page.getLayoutMetrics')
            return {'cssContentSize': {'width': 800, 'height': next(heights)}}
        self.driver.execute_cdp_cmd.side_effect = cdp
        with self.assertRaises(BalanceLayoutChangedError):
            self.capture_page()
        self.assertFalse(self.path.exists())
        self.assertLessEqual(self.driver.execute_cdp_cmd.call_count, 17)

    def test_resize_during_screenshot_preserves_existing_evidence(self):
        self.path.write_bytes(b'previous proof')
        captured = False
        def cdp(method, params):
            nonlocal captured
            if method == 'Page.getLayoutMetrics' and captured:
                return {'cssContentSize': {'width': 800, 'height': 1900}}
            output = self.cdp(method, params)
            if method == 'Page.captureScreenshot':
                captured = True
            return output
        self.driver.execute_cdp_cmd.side_effect = cdp
        with self.assertRaises(BalanceLayoutChangedError):
            self.capture_page()
        self.assertEqual(self.path.read_bytes(), b'previous proof')
        self.assertEqual(list(self.path.parent.glob('.*.png')), [])

    def test_navigation_during_resize_is_not_retryable_layout_error(self):
        def cdp(method, params):
            output = self.cdp(method, params)
            if method == 'Page.captureScreenshot':
                self.driver.current_url = 'https://sellercenter.lazada.co.th/apps/login'
            return output
        self.driver.execute_cdp_cmd.side_effect = cdp
        with self.assertRaises(BalanceEvidenceError) as error:
            self.capture_page()
        self.assertNotIsInstance(error.exception, BalanceLayoutChangedError)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
