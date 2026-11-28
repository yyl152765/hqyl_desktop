"""逐店下载 Lazada 活动模板，全部下载成功后按最低折扣率筛选。"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config_store import AppSettings, ConfigStore
from backend.services import lazada_bill_page as login, lazada_campaign_workbook as workbooks
from backend.services.lazada_balance_collector import COUNTRY_ALIASES, COUNTRY_DOMAINS
from backend.services.lazada_campaign_agreement import DIALOG, agreement_dialog, confirm_required_agreements
from backend.services.lazada_monthly_report import (
    ZiniaoPlaywrightRuntime, match_lazada_browsers, parse_store_names, safe_store_filename, validate_downloaded_report,
)

LOG = logging.getLogger("lazada_campaign")
HOME = "/portal/campaign/home"
MASTER = r"/portal/campaign/master/detail/?"
SCENE = r"/portal/campaign/scene/detail/\d+/?"
REFRESH_SECONDS = 120
ACTIVITY_WAIT_MS = 30_000
SCENE_CLICK_DELAY_MS = 5_000
SCENE_RETRY_AFTER_SECONDS = 10
DEFAULT_TIMEOUT_MINUTES = 30
REPRICE_EQUAL_PRICES = "reprice_equal_prices"
MENU = r"(?:用|通过)?\s*Excel\s*提交|Submit by Excel|Excel Submission|ส่งด้วย Excel"
INVITED_TAB = r"特别邀请|特邀活动|特殊邀请|Special Invitations?"
HISTORY = r"(?:导入\s*/\s*导出(?:历史|记录)|Import\s*/\s*Export (?:History|Records))(?:\s*>)?"
EXPORT_TAB = r"导出记录|Export Records"
DOWNLOAD = r"下\s*载|Download"
DOWNLOAD_EXCEL = r"下载\s*Excel|Download Excel|下载活动模板"


@dataclass(frozen=True)
class CampaignMode:
    title: str
    master_tab: str
    product_tab: str
    excel_menu: str


CAMPAIGN_MODES = {
    "invited": CampaignMode("特邀活动", INVITED_TAB, INVITED_TAB,
                            r"(?:用|通过)?\s*Excel\s*接受|Accept by Excel"),
    "unregistered": CampaignMode("未报名活动", r"未报名(?:的)?活动|未注册活动|Unregistered Campaigns?",
                                r"待提交|Pending Submission", MENU),
}


@dataclass(frozen=True)
class BrowserConfig:
    company: str
    username: str
    password: str = field(repr=False)
    client_path: str = ""
    socket_port: int = 16851
    browser_window_mode: str = "normal"


class ZiniaoAccountRequired(ValueError):
    pass


def resolve_browser_config(settings: AppSettings, account_id: str = "") -> BrowserConfig:
    selected = account_id or settings.active_account_ids.get("ziniao")
    account = next((a for a in settings.accounts if a.vendor == "ziniao" and a.id == selected), None)
    if account is None:
        raise ZiniaoAccountRequired("请先在桌面工具的账号设置中绑定并选中紫鸟账号")
    return BrowserConfig(account.extra.get("company") or account.name, account.username,
                         account.password, settings.lazada_bill_client_path or settings.lazada_ads_client_path)


def default_output_dir(settings: AppSettings) -> Path:
    return Path(settings.output_dir) / "Lazada活动模板"


@dataclass(frozen=True)
class StoreTarget:
    name: str
    site: str


@dataclass(frozen=True)
class DownloadOptions:
    stores: tuple[StoreTarget, ...]
    campaign: str
    scene: str
    output_dir: str
    timeout_minutes: int
    mode: str | None = None
    processing_mode: str = "auto"
    threshold: Decimal = workbooks.DEFAULT_THRESHOLD


def _workbook_settings(processing_mode: str) -> tuple[str, bool]:
    if processing_mode == REPRICE_EQUAL_PRICES:
        return "auto", True
    return processing_mode, False


def store_targets(payload: dict) -> tuple[StoreTarget, ...]:
    raw = payload.get("store_names", payload.get("store", ""))
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        raw = "\n".join(raw)
    if not isinstance(raw, str):
        raise ValueError("请每行填写一个紫鸟店铺名称")
    names = parse_store_names(raw.splitlines())
    if not names:
        raise ValueError("请填写店铺名称，每行一个")
    # Older single-store callers could supply an explicit site for a name without a country.
    explicit = site_code(payload["site"]) if payload.get("site") else ""
    if explicit and len(names) != 1:
        raise ValueError("多店铺下载按各自店名识别站点，请移除统一指定的站点")
    targets = []
    for name in names:
        sites = {code for alias, code in COUNTRY_ALIASES.items() if alias in name}
        if len(sites) > 1:
            raise ValueError(f"店铺「{name}」包含多个站点名称，请核对店名")
        if not sites and not explicit:
            raise ValueError(f"无法识别店铺「{name}」的站点，请填写含国家名称的紫鸟店名")
        site = next(iter(sites), explicit)
        if explicit and explicit != site:
            raise ValueError(f"店铺「{name}」识别为 {site}，与指定站点 {explicit} 不一致")
        targets.append(StoreTarget(name, site))
    return tuple(targets)


def download_options(payload: dict, settings: AppSettings) -> DownloadOptions:
    if not isinstance(payload, dict):
        raise ValueError("请填写下载条件")
    stores = store_targets(payload)
    mode = payload.get("mode")
    if mode is not None and (not isinstance(mode, str) or mode not in CAMPAIGN_MODES):
        raise ValueError("活动模式请选择特邀活动或未报名活动")
    names = {}
    for key, title in (("campaign", "首页活动名称"), ("scene", "子活动完整名称")):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写{title}")
        names[key] = value.strip()
    minutes = payload.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    if type(minutes) is not int or minutes <= 0:
        raise ValueError("最长等待时间必须是正整数分钟")
    folder = Path(payload.get("output_dir") or default_output_dir(settings)).expanduser().resolve()
    if folder.exists() and not folder.is_dir():
        raise ValueError("保存目录指向了文件，请选择文件夹")
    processing_mode = payload.get("processing_mode", "auto")
    template_mode, _ = _workbook_settings(processing_mode)
    _, threshold = workbooks.validate_processing_options(
        template_mode, payload.get("threshold", workbooks.DEFAULT_THRESHOLD)
    )
    return DownloadOptions(stores=stores, **names, output_dir=str(folder), timeout_minutes=minutes, mode=mode,
                           processing_mode=processing_mode, threshold=threshold)


@dataclass(frozen=True)
class Site:
    local_host: str
    cross_border_host: str

    @property
    def finance_url(self):
        # The shared local-login helper uses this property for its return URL.
        return f"https://{self.local_host}{HOME}"


def site_code(value):
    value = str(value or "").strip()
    code = COUNTRY_ALIASES.get(value, value.upper())
    if code not in COUNTRY_DOMAINS:
        raise ValueError("站点请填写 PH、TH、MY、VN、SG、ID 或对应国家名")
    return code


def target_host(site, current_url, store):
    host = urlsplit(current_url).hostname or ""
    if host == site.local_host:
        return host
    if (host in ("gsp.lazada-seller.cn", "gsp.lazada.com")
            or host in {f"sellercenter-{c.lower()}.lazada-seller.cn" for c in COUNTRY_DOMAINS}):
        return site.cross_border_host
    if host in COUNTRY_DOMAINS.values():
        raise ValueError("当前为另一国家的本土店，请核对店铺和目标站点")
    return site.cross_border_host if "跨境" in store else site.local_host


def on_route(page, host, path):
    url = urlsplit(page.url)
    return url.scheme == "https" and url.netloc == host and re.fullmatch(path, url.path) is not None


def open_home(page, country, store):
    site = Site(COUNTRY_DOMAINS[country], f"sellercenter-{country.lower()}.lazada-seller.cn")
    page = login.activate_lazada_from_launcher(page, store, site)
    host = target_host(site, page.url, store)
    state = {}
    # Limit the shared login helper, including its registration fallback, to the chosen host.
    login_site = Site(host, host)
    if urlsplit(page.url).hostname in {host, "gsp.lazada-seller.cn"}:
        page = login.complete_lazada_login(page, store, login_site, state)
    for _ in range(2):
        page.goto(f"https://{host}{HOME}", wait_until="domcontentloaded", timeout=120_000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except BrowserTimeout:
            pass
        page = login.complete_lazada_login(page, store, login_site, state)
        login.ensure_lazada_session(page)
        if on_route(page, host, HOME + "/?"):
            language = page.locator("html").get_attribute("lang") or ""
            if language.lower().split("-")[0] in {"th", "id", "vi", "ms"}:
                login.ensure_preferred_language(page, store, "简体中文")
            return page, host
    raise RuntimeError(f"登录后未进入 {country} 活动首页，请检查此店铺的站点权限")


def label(root, pattern):
    return root.get_by_text(re.compile(f"^(?:{pattern})$", re.I)).filter(visible=True)


def unique(locator, description, timeout=15_000):
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
    except BrowserTimeout:
        raise RuntimeError(f"未找到{description}，请核对页面名称和当前活动支持的操作") from None
    if locator.count() != 1:
        raise RuntimeError(f"{description}匹配到多个位置，请核对完整名称")
    return locator


def tab_name(pattern):
    return re.compile(rf"^(?:{pattern})(?:\s*[（(]\s*\d+\s*[）)])?$", re.I)


def tab_controls(root, name):
    return root.get_by_role("tab", name=name).or_(root.get_by_text(name).filter(visible=True))


def select_tab(root, name, description):
    try:
        tab_controls(root, name).first.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
    except BrowserTimeout:
        raise RuntimeError(f"未找到{description}") from None
    tab = root.get_by_role("tab", name=name)
    if not tab.count():
        bar = root.locator('[role="tablist"], .next-tabs-bar, .ant-tabs-nav')
        tab = bar.get_by_text(name).filter(visible=True)
    if not tab.count():
        tab = root.get_by_text(name).filter(visible=True)
    tab = unique(tab, description)
    control = tab.locator("xpath=ancestor-or-self::*[@role='tab' or @aria-controls or @aria-selected][1]")
    if control.count():
        tab = control
    page = tab.page
    try:
        selected = tab.get_attribute("aria-selected")
        panel_id = tab.get_attribute("aria-controls")
        if selected != "true":
            tab.click(timeout=ACTIVITY_WAIT_MS)
        if selected is not None:
            tab.and_(page.locator('[aria-selected="true"]')).wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
        if panel_id:
            panel = page.locator(f'[id={json.dumps(panel_id, ensure_ascii=False)}]')
            panel.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
            return panel
        tab_id = tab.get_attribute("id")
        if tab_id:
            panel = page.locator(f'[role="tabpanel"][aria-labelledby~={json.dumps(tab_id, ensure_ascii=False)}]')
            if panel.count():
                panel.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
                return panel
        # Some tab bars share a list below the controls and omit aria-controls.
        panels = tab.locator("xpath=ancestor::*[@role='tablist'][1]/following-sibling::*[@role='tabpanel']")
        if panels.count():
            return unique(panels.filter(visible=True), f"{description}的活动面板", timeout=ACTIVITY_WAIT_MS)
        return root
    except BrowserTimeout:
        raise RuntimeError(f"{description}未能切换，请核对活动页面是否已加载") from None


def _click_scene(root, title, description, *, retry=False):
    # Seller Center may render the title before the list or its click handler is ready.
    loading = title.locator("xpath=ancestor-or-self::*[@aria-busy='true']")
    if root is not title.page:
        loading = loading.or_(root.locator('[aria-busy="true"], .next-loading-tip, .ant-spin-spinning'))
    loading = loading.filter(visible=True)
    # Keep watching for the first click's navigation while the list is updating.
    if retry and (loading.count() or title.count() != 1):
        return False
    try:
        loading.first.wait_for(state="hidden", timeout=ACTIVITY_WAIT_MS)
        unique(title, description, timeout=ACTIVITY_WAIT_MS)
        control = title.locator(
            "xpath=ancestor-or-self::*[self::a or self::button or @role='link' or @role='button'][1]"
        )
        action = control if control.count() else title
        if retry and not action.is_enabled():
            return False
        action.click(timeout=ACTIVITY_WAIT_MS)
        return True
    except BrowserTimeout:
        raise RuntimeError(f"{description}尚未加载完成或无法点击") from None


def enter_activity(page, host, name, path, *, scope=None, description=None, progress=LOG.info):
    root = page if scope is None else scope
    target = root.get_by_text(name, exact=True).filter(visible=True)
    description = description or f"活动「{name}」"
    is_scene = path == SCENE
    if is_scene:
        try:
            target.first.scroll_into_view_if_needed(timeout=ACTIVITY_WAIT_MS)
        except BrowserTimeout:
            raise RuntimeError(f"未找到{description}，请核对页面名称和活动模式") from None
        progress(f"已找到子活动「{name}」，等待 {SCENE_CLICK_DELAY_MS / 1000:g} 秒，页面就绪后自动点击")
        page.wait_for_timeout(SCENE_CLICK_DELAY_MS)

    previous = set(page.context.pages)
    source_url = page.url
    may_retry = is_scene and on_route(page, host, MASTER)
    agreements = agreement_dialog(page) if may_retry else None
    confirmed = False

    def note_navigation(request):
        nonlocal may_retry
        if not may_retry or not request.is_navigation_request():
            return
        try:
            frame = request.frame
        except BrowserError:
            # A new popup can request its document before Playwright knows its frame.
            if urlsplit(request.url).hostname == host:
                may_retry = False
            return
        owner = frame.page
        if frame.parent_frame is None and (owner is page or (owner not in previous and owner.opener() is page)):
            may_retry = False

    if is_scene:
        page.context.on("request", note_navigation)
    try:
        if is_scene:
            _click_scene(root, target, description)
            progress(f"已自动点击子活动「{name}」，正在等待详情页")
        else:
            unique(target, description, timeout=ACTIVITY_WAIT_MS).click()
        clicked_at = time.monotonic()
        deadline = clicked_at + ACTIVITY_WAIT_MS / 1000
        while time.monotonic() < deadline:
            opened = [p for p in page.context.pages if p not in previous and not p.is_closed() and p.opener() is page]
            if agreements is not None and page.url == source_url and agreements.count():
                if confirmed:
                    raise RuntimeError("确认后再次出现大促协议，请查看该活动的协议状态")
                # Some campaigns navigate on confirmation; others unlock the same scene card.
                may_retry = not opened
                confirm_required_agreements(agreements, progress)
                confirmed = True
                clicked_at = time.monotonic()
                deadline = clicked_at + ACTIVITY_WAIT_MS / 1000
                continue
            candidates = [p for p in [page, *opened] if on_route(p, host, path)]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise RuntimeError("同一活动打开了多个详情页，请核对活动名称")
            if opened or page.url != source_url:
                may_retry = False
            if may_retry and time.monotonic() - clicked_at >= SCENE_RETRY_AFTER_SECONDS:
                if _click_scene(root, target, description, retry=True):
                    may_retry = False
                    reason = "协议确认后仍在列表" if confirmed else "首次点击后仍在列表"
                    progress(f"{reason}，已重新定位并自动点击「{name}」一次")
                    deadline = time.monotonic() + ACTIVITY_WAIT_MS / 1000
            page.wait_for_timeout(200)
    finally:
        if is_scene:
            page.context.remove_listener("request", note_navigation)
    raise RuntimeError(f"点击「{name}」后未进入所选站点的活动详情页")


def excel_menu(page, mode, *, agreements=None):
    ready = label(page, MENU)
    if mode:
        product_tab = tab_name(mode.product_tab)
        product_section = tab_name(r"商品提交|提交产品|Product Submission")
        section = tab_controls(page, product_section)
        menus = f"{mode.excel_menu}|{MENU}"
        ready = tab_controls(page, product_tab).or_(label(page, menus)).or_(section)
    try:
        if mode or agreements is not None:
            entry = ready.or_(agreements) if agreements is not None else ready
            if mode is None:
                # Older activity pages can open directly on the Excel dialog.
                entry = entry.or_(history_dialog(page)).or_(download_confirmation(page)).or_(label(page, HISTORY))
            entry.first.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
        if agreements is not None and agreements.count():
            return None
        if mode is None:
            return ready
        root = select_tab(page, product_section, "商品提交栏目") if section.count() else page
        tab = tab_controls(root, product_tab)
        tab.or_(label(root, menus)).first.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
        if tab.count():
            root = select_tab(root, product_tab, f"「{mode.title}」的商品标签")
        preferred = label(root, mode.excel_menu)
        label(root, menus).first.wait_for(state="visible", timeout=ACTIVITY_WAIT_MS)
        return preferred if preferred.count() else label(root, MENU)
    except BrowserTimeout:
        raise RuntimeError(f"未能打开「{mode.title if mode else '子活动'}」的 Excel 菜单，请核对商品提交页面") from None


def history_dialog(page):
    return page.locator(DIALOG).filter(visible=True).filter(has=label(page, EXPORT_TAB))


def download_confirmation(page):
    options = page.locator(DIALOG).filter(visible=True).filter(has_not=label(page, EXPORT_TAB))
    return label(options, DOWNLOAD)


def open_history(page, menu, *, allow_direct=False):
    dialog = history_dialog(page)
    if not dialog.count():
        history = label(page, HISTORY)
        direct = download_confirmation(page) if allow_direct else None
        if not history.count() and not (direct is not None and direct.count()):
            unique(menu, "Excel 菜单").click()
        choices = history.or_(dialog)
        if direct is not None:
            choices = choices.or_(direct)
        try:
            choices.first.wait_for(state="visible", timeout=15_000)
        except BrowserTimeout:
            raise RuntimeError("Excel 窗口中未找到导入/导出历史入口") from None
        if not dialog.count():
            if allow_direct and not history.count():
                return None
            unique(history, "导入/导出历史入口").click()
    unique(dialog, "历史文件窗口")
    unique(label(dialog, EXPORT_TAB), "导出记录").click()
    return dialog


def close_history(dialog):
    close = dialog.locator(
        '.next-dialog-close, .ant-modal-close, [class*="dialog-close"], '
        '[class*="modal-close"], [aria-label="Close" i], [aria-label="关闭"], [title="关闭"]'
    ).filter(visible=True)
    # Some close buttons contain an icon which also matches the selector.
    close.first.click(timeout=5000)
    dialog.wait_for(state="hidden", timeout=5000)


def history_rows(dialog):
    rows = dialog.locator("tbody tr, [role=row]").filter(visible=True)
    empty = label(dialog, r"Total\s*0|共\s*0\s*条|暂无数据|没有数据|无数据|No (?:data|records)(?: available)?")
    loading = dialog.locator('[aria-busy=true], .ant-spin-spinning, .next-loading-tip').filter(visible=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if loading.count():
            dialog.page.wait_for_timeout(200)
            continue
        records = {}
        for row in rows.all():
            cells = row.locator("td, [role=cell]")
            if cells.count() >= 5:
                task = cells.first.inner_text().strip()
                if not re.fullmatch(r"\d+", task) or task in records:
                    raise RuntimeError("导出记录任务 ID 无法唯一识别")
                # Bind subsequent actions to this ID even if the table reorders.
                records[task] = rows.filter(has=dialog.page.locator(
                    "td:first-child, [role=cell]:first-child"
                ).filter(has_text=re.compile(rf"^\s*{task}\s*$")))
        if records:
            return records
        if empty.count():
            return {}
        dialog.page.wait_for_timeout(200)
    raise RuntimeError("未能读取导出记录，请核对历史文件表格是否已加载")


def request_export(page, menu, downloads):
    item = label(page, DOWNLOAD_EXCEL)
    confirm = download_confirmation(page)
    if not confirm.count() and not item.count():
        unique(menu, "Excel 菜单").click()
    try:
        item.or_(confirm).first.wait_for(state="visible", timeout=15_000)
    except BrowserTimeout:
        raise RuntimeError("Excel 窗口中未找到模板下载入口") from None
    if confirm.count():
        unique(confirm, "模板下载确认").click()
        return
    unique(item, "下载 Excel 菜单项").click()
    # The selection can download a file, queue a task, or open one last options dialog.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if downloads or history_dialog(page).count():
            return
        if confirm.count():
            unique(confirm, "模板下载确认").click()
            return
        page.wait_for_timeout(200)


def export_template(page, minutes, progress=LOG.info, *, mode=None):
    scene_url = page.url
    agreements = agreement_dialog(page)
    confirmed = False
    while True:
        # A scene may render its agreement in the new tab, before (or over) its Excel menu.
        try:
            menu = excel_menu(page, mode, agreements=agreements)
            if menu is not None:
                dialog = open_history(page, menu, allow_direct=True)
                break
        except (BrowserError, RuntimeError):
            if not agreements.count():
                raise
        if confirmed or page.url != scene_url:
            raise RuntimeError("子活动页面或协议状态已变化，无法继续确认大促协议")
        confirm_required_agreements(agreements, progress)
        confirmed = True
    existing = None
    if dialog is None:
        progress("活动提供直接下载，正在接收 Excel 模板")
    else:
        existing = set(history_rows(dialog))
        close_history(dialog)

    downloads = []

    def on_download(download):
        downloads.append(download)

    page.on("download", on_download)
    try:
        request_export(page, menu, downloads)
        task = None
        checked_at = time.monotonic()
        deadline = checked_at + minutes * 60
        while True:
            if downloads:
                return downloads[0]
            if page.url != scene_url:
                raise RuntimeError("活动详情地址发生变化，已停止检查导出记录")
            if existing is None:
                # Without an initial history, only a download event belongs unambiguously to this request.
                if history_dialog(page).count():
                    raise RuntimeError("下载后才出现历史文件窗口，无法确认本次导出任务 ID，请核对该活动的导出记录")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"直接下载窗口在 {minutes} 分钟内未返回 Excel 文件")
                page.wait_for_timeout(250)
                continue
            dialog = open_history(page, menu)
            records = history_rows(dialog)
            if task is None:
                new_tasks = records.keys() - existing
                if len(new_tasks) > 1:
                    raise RuntimeError("本次出现多个新任务，无法确定对应文件，请核对导出记录")
                task = next(iter(new_tasks), None)
            if task in records:
                row = records[task]
                if label(row, r"失败|Failed|Failure").count():
                    raise RuntimeError(f"活动模板导出失败，任务 ID {task}")
                if label(row, r"成功|Success(?:ful)?|Succeeded").count():
                    link = unique(label(row, DOWNLOAD), "本次任务的下载按钮")
                    with page.expect_download(timeout=60_000) as received:
                        link.click()
                    return received.value
            next_refresh = checked_at + REFRESH_SECONDS
            wait_until = min(deadline, next_refresh)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待导出超过 {minutes} 分钟，任务 ID {task or '尚未显示'}，请查看该活动的导出记录")
            progress(f"Excel 仍在生成（任务 {task or '尚未显示'}），每两分钟检查一次")
            page.wait_for_timeout(max(0, wait_until - time.monotonic()) * 1000)
            if downloads:
                return downloads[0]
            # The history overlay must actually disappear before every page reload.
            close_history(dialog)
            if next_refresh > deadline:
                raise RuntimeError(f"等待导出超过 {minutes} 分钟，请查看该活动的导出记录")
            if page.url != scene_url:
                raise RuntimeError("刷新前活动详情地址发生变化")
            checked_at = time.monotonic()
            page.reload(wait_until="domcontentloaded", timeout=120_000)
            if page.url != scene_url:
                raise RuntimeError("刷新后活动详情地址发生变化")
            menu = excel_menu(page, mode)
    finally:
        page.remove_listener("download", on_download)


def save_template(download, staging, folder, store, country, scene):
    suffix = Path(download.suggested_filename).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        raise ValueError("下载结果不是 Excel 文件")
    temp = staging / f"template{suffix}"
    download.save_as(temp)
    validate_downloaded_report(temp)
    basename = safe_store_filename(scene, max_length=80)
    destination = folder / safe_store_filename(store, max_length=50) / country
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / f"{basename}_{uuid4().hex[:12]}{suffix}"
    return temp.rename(final)


def _download_store(runtime, browser, site, options, progress) -> Path:
    store = browser["browserName"]
    folder = Path(options.output_dir)
    mode = CAMPAIGN_MODES.get(options.mode)
    with TemporaryDirectory(dir=folder) as temporary:
        staging = Path(temporary)
        opened = runtime.open_browser(browser, staging)
        try:
            home, host = open_home(opened.page, site, store)
            progress(f"已进入 {site} 站（{host}），查找首页活动「{options.campaign}」")
            master = enter_activity(home, host, options.campaign, MASTER)
            master.wait_for_load_state("domcontentloaded", timeout=ACTIVITY_WAIT_MS)
            scope = master
            location = f"{site} 站「{options.campaign}」"
            if mode:
                location += f"下的「{mode.title}」"
                progress(f"正在进入{location}")
                scope = select_tab(master, tab_name(mode.master_tab), f"{location}标签")
            description = f"{location}中的子活动「{options.scene}」"
            progress(f"正在查找{description}")
            detail = enter_activity(master, host, options.scene, SCENE, scope=scope, description=description,
                                    progress=progress)
            progress(f"正在导出{mode.title if mode else '子活动'}的 Excel 模板")
            download = export_template(detail, options.timeout_minutes, progress, mode=mode)
            filename = f"{mode.title}_{options.scene}" if mode else options.scene
            return save_template(download, staging, folder, store, site, filename)
        finally:
            runtime.close_browser(opened)


def _error_message(exc, config):
    message = login._safe_error_detail(exc)
    for secret in (config.password, config.username):
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message


def _filter_downloads(stores: list[dict], options: DownloadOptions, progress: Callable[[str], None]) -> dict:
    progress(f"开始活动处理，最低折扣率 {options.threshold:%}，共 {len(stores)} 份模板")
    template_mode, reprice_equal_prices = _workbook_settings(options.processing_mode)
    if reprice_equal_prices:
        progress(f"已启用原价提报，沿用的活动价等于原价时按原价 × {options.threshold:%} 改价")
    root = Path(options.output_dir)
    multiple_batches = len(stores) > workbooks.MAX_FILES
    if multiple_batches:
        root /= f"活动处理_{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    location = ""
    errors = []
    for offset in range(0, len(stores), workbooks.MAX_FILES):
        group = stores[offset:offset + workbooks.MAX_FILES]
        sources = tuple(Path(store["output_file"]) for store in group)
        job = workbooks.CampaignWorkbookJob(sources, root, mode=template_mode, threshold=options.threshold,
                                            reprice_equal_prices=reprice_equal_prices)
        try:
            batch = workbooks.filter_campaign_workbooks(job, progress, create_archive=False)
        except Exception as exc:
            for store in group:
                store["processing_error"] = str(exc)
            progress(f"第 {offset + 1} 至 {offset + len(group)} 份模板处理失败：{exc}")
            errors.append(str(exc))
            continue
        location = str(root) if multiple_batches else batch["output_dir"]
        if batch["completion_message"]:
            errors.append(batch["completion_message"])
        for store, result in zip(group, batch["files"], strict=True):
            if result["ok"]:
                store["processed_file"] = result["processed_file"]
                progress(f"{store['store']} 已筛选，保留 {result['kept_rows']} 行：{result['processed_file']}")
            else:
                store["processing_error"] = result["error"]
                progress(f"{store['store']} 活动处理失败：{result['error']}")
    success = sum(bool(store.get("processed_file")) for store in stores)
    if location:
        progress(f"筛选结果目录：{location}")
    message = f"活动处理成功 {success} 份，失败 {len(stores) - success} 份"
    return {"success_count": success, "failed_count": len(stores) - success, "output_dir": location,
            "is_complete": success == len(stores) and not errors, "message": "。".join([message, *errors])}


def run_download(options: DownloadOptions, config: BrowserConfig, progress: Callable[[str], None] = LOG.info) -> dict:
    folder = Path(options.output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    runtime = ZiniaoPlaywrightRuntime(config, LOG)
    results = []
    files = []
    try:
        progress(f"正在匹配 {len(options.stores)} 家紫鸟店铺")
        runtime.start()
        matches, failures = match_lazada_browsers(runtime.list_browsers(), [s.name for s in options.stores])
        browsers, unmatched = dict(matches), dict(failures)
        for index, store in enumerate(options.stores, start=1):
            result = {"store": store.name, "site": store.site}
            progress(f"[{index}/{len(options.stores)}] {store.name} → {store.site}")
            try:
                if store.name in unmatched:
                    raise ValueError(unmatched[store.name])
                path = _download_store(runtime, browsers[store.name], store.site, options, progress)
            except Exception as exc:
                result["error"] = _error_message(exc, config)
                progress(f"{store.name} 下载失败：{result['error']}")
            else:
                files.append(path)
                result["output_file"] = str(path)
                progress(f"已保存：{path}")
            results.append(result)
    except Exception as exc:
        raise RuntimeError(_error_message(exc, config)) from None
    finally:
        runtime.shutdown()

    failed = len(results) - len(files)
    download_summary = f"模板下载完成，成功 {len(files)} 家，失败 {failed} 家"
    progress(download_summary)
    processing = None
    if files and not failed:
        processing = _filter_downloads(results, options, progress)
        processing_summary = processing["message"]
    else:
        processing_summary = "本批未全部下载成功，已跳过活动处理"
    progress(processing_summary)
    summary = f"{download_summary}。{processing_summary}"
    complete = bool(processing and processing["is_complete"])
    single_file = files[0] if len(results) == 1 and files else None
    return {
        "success": complete, "success_store_count": len(files), "failed_store_count": failed,
        "message": summary, "stores": results, "processing": processing,
        "output_file": str(single_file) if single_file else "",
        "output_dir": str(single_file.parent if single_file else folder) if files else "",
        "is_complete": complete, "completion_message": summary,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", dest="store_names", action="append", help="紫鸟店铺名称，可重复指定或每行一个")
    parser.add_argument("--site", help="兼容旧调用，仅用于指定单个店铺的站点")
    parser.add_argument("--mode", choices=CAMPAIGN_MODES, help="invited 为特邀活动，unregistered 为未报名活动，省略时沿用页面当前分类")
    parser.add_argument("--processing-mode", choices=sorted(workbooks.MODES | {REPRICE_EQUAL_PRICES}), default="auto",
                        help="Excel 处理方式，默认自动识别。reprice_equal_prices 按自定义折扣调整原价提报商品")
    parser.add_argument("--discount-percent", default="95", help="最低折扣率，按百分数填写，默认 95")
    prompts = {"campaign": "首页活动名称（例如 A+ Other）", "scene": "子活动完整名称（含前缀和日期）"}
    for key, title in prompts.items():
        parser.add_argument(f"--{key}", help=title)
    parser.add_argument("--output", dest="output_dir", help="保存目录（默认在桌面工具输出目录下的 Lazada活动模板）")
    parser.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES, help="最长生成等待分钟数，默认 30")
    args = vars(parser.parse_args(argv))
    try:
        args["threshold"] = Decimal(args.pop("discount_percent").strip().removesuffix("%")) / 100
    except InvalidOperation:
        parser.error("--discount-percent 请输入百分数，例如 95")
    if args["timeout_minutes"] <= 0:
        parser.error("--timeout-minutes 必须大于 0")
    if args["mode"] is None and sys.stdin.isatty() and not all(args[k] for k in ("store_names", "campaign", "scene")):
        choice = input("活动模式（1 特邀活动，2 未报名活动，回车选 1）：").strip() or "1"
        if choice not in ("1", "2"):
            parser.error("活动模式请输入 1 或 2")
        args["mode"] = "invited" if choice == "1" else "unregistered"
    if not args["store_names"]:
        print("请输入紫鸟店铺名称，每行一个，最后输入空行结束：")
        args["store_names"] = []
        while name := input().strip():
            args["store_names"].append(name)
    for key, title in prompts.items():
        if not args[key]:
            args[key] = input(f"{title}：")
    settings = ConfigStore().load()
    result = run_download(download_options(args, settings), resolve_browser_config(settings), progress=print)
    if not result["success"]:
        raise RuntimeError(result["message"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("已取消下载")
    except Exception as error:
        sys.exit(f"下载失败：{error}")
