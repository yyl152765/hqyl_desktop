"""Capture an exact ZiNiao store window with its original browser chrome."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from uuid import uuid4


class BalanceEvidenceError(ValueError):
    pass


def _native_window_image(hwnd: int, width: int, height: int):
    from ctypes import wintypes as w
    from PIL import Image

    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    bindings = [
        (user, "GetWindowDC", [w.HWND], w.HDC),
        (user, "ReleaseDC", [w.HWND, w.HDC], ctypes.c_int),
        (user, "PrintWindow", [w.HWND, w.HDC, w.UINT], w.BOOL),
        (gdi, "CreateCompatibleDC", [w.HDC], w.HDC),
        (gdi, "CreateCompatibleBitmap", [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
        (gdi, "SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
        (gdi, "DeleteObject", [w.HANDLE], w.BOOL),
        (gdi, "DeleteDC", [w.HDC], w.BOOL),
    ]
    for library, name, args, result in bindings:
        function = getattr(library, name)
        function.argtypes, function.restype = args, result

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                    ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                    ("image_size", w.DWORD), ("xppm", w.LONG), ("yppm", w.LONG),
                    ("colors", w.DWORD), ("important", w.DWORD)]

    gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p,
                              ctypes.POINTER(BitmapInfo), w.UINT]
    gdi.GetDIBits.restype = ctypes.c_int
    source = memory = bitmap = previous = None
    try:
        source = user.GetWindowDC(hwnd)
        memory = gdi.CreateCompatibleDC(source)
        bitmap = gdi.CreateCompatibleBitmap(source, width, height)
        if not all((source, memory, bitmap)):
            raise BalanceEvidenceError("无法建立紫鸟窗口截图")
        previous = gdi.SelectObject(memory, bitmap)
        if not user.PrintWindow(hwnd, memory, 2):
            raise BalanceEvidenceError("紫鸟窗口截图失败，请保持店铺窗口正常显示")
        gdi.SelectObject(memory, previous)
        previous = None
        info = BitmapInfo(ctypes.sizeof(BitmapInfo), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise BalanceEvidenceError("紫鸟窗口截图数据不完整")
        return Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
    finally:
        if previous and memory:
            gdi.SelectObject(memory, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory:
            gdi.DeleteDC(memory)
        if source:
            user.ReleaseDC(hwnd, source)


def capture_balance_evidence(driver, path: str | Path, store_name: str, field_label: str,
                             *, window_api=None, capture_window=None) -> str:
    """Save an unaltered full-window image for exactly one verified store.

    PrintWindow captures this HWND independently of other store windows. A page
    screenshot cannot include ZiNiao's store label and is not a fallback here.
    The full native window title verifies identity; ZiNiao can still visually
    abbreviate its store chip, so consumers must label any added provenance.
    The adapters are injectable so ordinary tests never touch the desktop.
    """
    if not store_name or not field_label:
        raise BalanceEvidenceError("截图缺少店铺或指标名称")
    if window_api is None:
        if os.name != "nt":
            raise BalanceEvidenceError("含紫鸟店铺名称的窗口截图需要 Windows")
        from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
        _ensure_superbrowser_path()
        from util.ziniao_window_position import Win32WindowApi
        window_api = Win32WindowApi()
    from PIL import ImageStat

    matches = [item for item in window_api.find_windows(store_name)
               if item.title == store_name and item.visible and item.top_level
               and item.process_name.casefold() == "ziniaobrowser.exe"]
    if len(matches) != 1:
        raise BalanceEvidenceError("无法唯一确认带有完整店铺名称的紫鸟窗口")
    identity = matches[0]
    placement = window_api.placement(identity.hwnd)
    if placement.show_cmd == 2:
        raise BalanceEvidenceError("店铺窗口已最小化，请恢复后重新采集截图")
    rect = window_api.rect(identity.hwnd)
    if not (400 <= rect.width <= 12000 and 250 <= rect.height <= 8000):
        raise BalanceEvidenceError("紫鸟窗口尺寸不足以完整显示金额和店铺名称")
    page_url = driver.current_url
    if driver.execute_script("return document.visibilityState === 'visible';") is not True:
        raise BalanceEvidenceError("资金页面不是当前可见页签，不能截取其他页面作为凭证")
    image = (capture_window or _native_window_image)(identity.hwnd, rect.width, rect.height)
    try:
        if image.size != (rect.width, rect.height) or max(ImageStat.Stat(image).stddev) < 2:
            raise BalanceEvidenceError("紫鸟截图为空白，不能作为金额取数凭证")
        # The source page must also render, not merely a colorful browser frame.
        content = image.crop((0, min(160, rect.height // 3), rect.width, rect.height))
        try:
            if max(ImageStat.Stat(content).stddev) < 2:
                raise BalanceEvidenceError("紫鸟页面截图为空白，请恢复窗口后重试")
        finally:
            content.close()
        if window_api.identity(identity.hwnd) != identity or driver.current_url != page_url:
            raise BalanceEvidenceError("截图过程中店铺窗口或页面发生变化，请重试")
        if driver.execute_script("return document.visibilityState === 'visible';") is not True:
            raise BalanceEvidenceError("截图过程中资金页签已隐藏，请切回目标页面后重试")
        if window_api.placement(identity.hwnd).show_cmd != placement.show_cmd or window_api.rect(identity.hwnd) != rect:
            raise BalanceEvidenceError("截图过程中店铺窗口尺寸、位置或显示状态发生变化，请重试")
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.png")
        try:
            image.save(temporary, "PNG")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)
    finally:
        image.close()
