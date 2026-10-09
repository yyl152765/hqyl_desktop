from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from PIL import Image, ImageDraw

from backend.services.balance_evidence import BalanceEvidenceError, capture_balance_evidence


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


if __name__ == "__main__":
    unittest.main()
