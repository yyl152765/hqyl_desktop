"""选择 Lazada 大促协议弹窗中明确标为必选的复选框。"""
import re
import time
from collections.abc import Callable

from playwright.sync_api import Error as BrowserError, Locator, Page, TimeoutError as BrowserTimeout


DIALOG = 'dialog[open], [role="dialog"], [role="alertdialog"], .next-dialog, .ant-modal, .eds-dialog'
_TITLES = r"大促协议|活动协议|签署协议|(?:Mega\s+)?Campaign Agreements?|Promotion Agreements?|Sign Agreements?"
TITLE = re.compile(rf"^(?:{_TITLES})$", re.I)
CONFIRM = re.compile(r"^(?:确定|确认|Confirm|OK)$", re.I)


def agreement_dialog(page: Page) -> Locator:
    named = page.get_by_role("dialog", name=TITLE).or_(page.get_by_role("alertdialog", name=TITLE))
    dialogs = page.locator(DIALOG).filter(visible=True)
    heading = page.get_by_role("heading", name=TITLE).or_(page.locator(
        ".next-dialog-header, .ant-modal-title, .eds-dialog__title"
    ).filter(has_text=TITLE).filter(visible=True))
    # Some versions use a plain div for the first line. A link to terms in
    # the body of an unrelated dialog must not identify it as an agreement.
    plain_title = dialogs.filter(has=page.get_by_text(TITLE).filter(visible=True)).filter(
        has_text=re.compile(rf"^\s*[×✕]?\s*(?:{_TITLES})", re.I)
    )
    matching = named.or_(dialogs.filter(has=heading)).or_(plain_title)
    # A component may put both a class and an ARIA dialog on nested elements.
    return matching.filter(has_not=matching)


# Read one consistent snapshot. Return the actual control (or its associated label),
# so reordering the agreement cards cannot redirect a click to another checkbox.
_READ_CHOICES = r"""
dialog => {
    const visible = node => node && node.getClientRects().length > 0 &&
        !['hidden', 'collapse'].includes(getComputedStyle(node).visibility);
    const requiredBadge = text => /^(?:必选|必選|必填|强制|mandatory|required)$/i.test(text.trim()) ||
        /[（(\[]\s*(?:必选|必選|必填|强制|mandatory|required)\s*[）)\]]$/i.test(text.trim());
    const loading = '[aria-busy="true"], .next-loading-tip, .ant-spin-spinning';
    if (dialog.matches('[aria-busy="true"]') || [...dialog.querySelectorAll(loading)].some(visible))
        return {info: {waiting: '协议内容仍在加载'}};

    const boxes = [...dialog.querySelectorAll('input[type="checkbox"], [role="checkbox"]')]
        .filter(box => !box.querySelector('input[type="checkbox"]'));
    if (!boxes.length) return {info: {waiting: '尚未显示协议复选框'}};
    const rows = boxes.map(box => {
        const wrapper = box.closest('[role="checkbox"]');
        const labels = [...(box.labels || [])].filter(label => dialog.contains(label));
        const target = visible(box) && getComputedStyle(box).pointerEvents !== 'none' ? box :
            labels.find(visible) || (visible(wrapper) ? wrapper : null);
        const native = box.tagName === 'INPUT';
        const state = native ? (box.indeterminate ? 'mixed' : String(box.checked)) : box.getAttribute('aria-checked');
        const required = box.required === true || [box, wrapper].some(node => node &&
            (node.getAttribute('aria-required') === 'true' || requiredBadge(node.getAttribute('aria-label') || '')));
        const disabled = box.matches(':disabled') || !!box.closest('[aria-disabled="true"]');
        const references = [box, wrapper].filter(Boolean).flatMap(node =>
            ['aria-labelledby', 'aria-describedby'].flatMap(attr => (node.getAttribute(attr) || '').split(/\s+/)));
        return {box, target, labels, references, required, state, disabled};
    });

    // A badge belongs to its labelled control or the nearest row containing a
    // single checkbox. A badge on a parent card never makes all its children required.
    for (const badge of dialog.querySelectorAll('*')) {
        if (!visible(badge) || !requiredBadge(badge.textContent) ||
            [...badge.children].some(child => requiredBadge(child.textContent))) continue;
        let assigned = false;
        for (let row = badge; row && row !== dialog; row = row.parentElement) {
            if (row.matches('[role="radio"], [role="radiogroup"]') || row.control?.type === 'radio') {
                assigned = true;
                break;
            }
            const linked = rows.filter(choice => choice.labels.includes(row) ||
                (row.id && choice.references.includes(row.id)));
            const candidates = linked.length ? linked : rows.filter(choice =>
                row.contains(choice.box) || choice.labels.some(label => row.contains(label)));
            if (!linked.length && row.querySelector('input[type="radio"], [role="radio"]')) {
                // A required radio-group label does not select an optional agreement.
                assigned = candidates.length === 0;
                break;
            }
            if (candidates.length > 1) break;
            if (candidates.length === 1) {
                candidates[0].required = true;
                assigned = true;
                break;
            }
            if (row.matches('section, article, fieldset, tr, li, [role="group"]')) break;
        }
        if (!assigned) return {info: {error: '无法将「必选」标记唯一对应到协议选项'}};
    }

    const wrong = rows.filter(row => row.state !== String(row.required));
    const usable = row => row.target && !row.disabled && ['true', 'false'].includes(row.state);
    // Clear checked children before their optional parent can collapse or disable them.
    const next = wrong.slice().reverse().find(row => !row.required && usable(row)) ||
        wrong.find(row => row.required && usable(row));
    if (wrong.length && !next) return {info: {error: '有协议选项的勾选状态无法读取或调整'}};
    return {
        target: next?.target,
        info: {
            desired: next ? next.required : null,
            required: rows.filter(row => row.required).length,
            total: rows.length,
            signature: rows.map(row => (row.required ? 'R' : 'O') + row.state).join('/')
        }
    };
}
"""


def confirm_required_agreements(dialog: Locator, progress: Callable[[str], None], *, timeout_ms=20_000) -> None:
    if dialog.count() != 1:
        raise RuntimeError("无法唯一识别大促协议窗口")
    progress("检测到大促协议，正在核对必选项并清除其他勾选")
    page = dialog.page
    activity_url = page.url
    buttons = dialog.get_by_role("button", name=CONFIRM)
    clean_signature = None
    attempts = {}
    waiting = "正在等待协议内容"
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        wait_ms = 150
        snapshot = dialog.evaluate_handle(_READ_CHOICES, timeout=remaining)
        try:
            if page.url != activity_url:
                raise RuntimeError("大促协议未确认：当前活动页面已变化")
            info = snapshot.evaluate("plan => plan.info")
            if "error" in info:
                raise RuntimeError(f"大促协议未确认：{info['error']}")
            if "waiting" in info:
                waiting, clean_signature = info["waiting"], None
            elif info["desired"] is not None:
                signature = info["signature"]
                if attempts.get(signature, 0) >= 2:
                    raise RuntimeError("大促协议未确认：页面反复改变勾选状态")
                target = snapshot.get_property("target").as_element()
                try:
                    target.set_checked(info["desired"], timeout=min(remaining, 4000))
                    attempts[signature] = attempts.get(signature, 0) + 1
                except BrowserError:
                    if target.evaluate("element => element.isConnected"):
                        raise RuntimeError("大促协议未确认：无法将复选框设为所需状态") from None
                    # The card was replaced. Classify the new DOM before acting again.
                finally:
                    target.dispose()
                clean_signature = None
            else:
                confirm = buttons if buttons.count() else dialog.get_by_text(CONFIRM).filter(visible=True)
                if confirm.count() > 1:
                    raise RuntimeError("大促协议未确认：存在多个确定按钮")
                if confirm.count() and confirm.is_enabled():
                    if clean_signature == info["signature"]:
                        confirm.click(timeout=min(remaining, 4000))
                        try:
                            dialog.wait_for(state="hidden", timeout=timeout_ms)
                        except BrowserTimeout:
                            raise RuntimeError("已点击确定，但大促协议窗口仍然打开，请查看页面提示") from None
                        mandatory = info["required"]
                        progress(f"大促协议已确认，必选 {mandatory} 项已勾选，其他 {info['total'] - mandatory} 项均未勾选")
                        return
                    # Scroll the button into view, then take a fresh snapshot before confirming.
                    confirm.click(trial=True, timeout=min(remaining, 4000))
                    clean_signature = info["signature"]
                    wait_ms = 500
                else:
                    waiting, clean_signature = "确定按钮尚不可用", None
        finally:
            snapshot.dispose()
        page.wait_for_timeout(min(remaining, wait_ms))
    raise RuntimeError(f"大促协议未确认：{waiting}")
