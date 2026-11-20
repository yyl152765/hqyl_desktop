"""Find the earliest live Shopee listing, falling back to unlisted only when empty."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill

from backend.services.bigseller_claim_query import parse_listing_time
from backend.services.bigseller_item_id_query import (
    BigSellerAuthenticationError,
    BigSellerItemIdQueryJob,
    BigSellerPermissionError,
    BigSellerQueryError,
    build_listing_query,
    first_item_id,
    login_for_query,
    query_process_config,
    validate_bigseller_item_id_query_payload,
)
from backend.services.bigseller_sku_benchmark import BenchmarkCooldownError, request_benchmark_page
from backend.services.bigseller_sync import load_bigseller_request_util

ProgressCallback = Callable[[str], None]
MAX_SKU_COUNT = 50_000
PREVIEW_ROW_LIMIT = 500
PAGE_SIZE = 50
MAX_PAGES = 500
SITE_LABELS = {"PH": "菲律宾", "ID": "印尼", "MY": "马来西亚", "VN": "越南", "TH": "泰国",
               "SG": "新加坡", "TW": "台湾", "BR": "巴西", "MX": "墨西哥", "CO": "哥伦比亚", "CL": "智利"}
LISTING_SCOPE_LABELS = {"live": "在售", "unlisted": "下架"}
SHOPEE_STATUSES = {"live": "live", "unlisted": "unList"}
SHOPEE_HOSTS = {
    "shopee.co.id", "shopee.ph", "shopee.com.my", "shopee.vn", "shopee.co.th",
    "shopee.sg", "shopee.tw", "shopee.com.br", "shopee.com.mx", "shopee.com.co", "shopee.cl",
}
OUTPUT_COLUMNS = (
    ("sku", "SKU", 30), ("shop_name", "店铺名", 36), ("product_url", "商品链接", 60),
    ("listed_time", "上架时间", 24), ("item_id", "Item ID", 26),
    ("listing_scope", "商品状态", 14),
    ("status", "查询状态", 16), ("message", "说明", 60),
)
STATUS_LABELS = {"matched": "已匹配", "not_found": "未找到商品", "failed": "查询失败"}
QUERY_RULE = "仅查询所选国家；SKU（含子 SKU）模糊搜索；先查在售，无匹配再查下架；在命中范围内按平台创建时间升序取首条"


@dataclass(frozen=True)
class BigSellerLinkQueryJob(BigSellerItemIdQueryJob):
    site: str = "PH"


def _validate_site(value: Any) -> str:
    site = str(value or "PH").strip().upper()
    if site not in SITE_LABELS:
        raise ValueError("请选择有效的 Shopee 国家/地区")
    return site


def validate_bigseller_link_query_payload(payload: dict[str, Any]) -> BigSellerLinkQueryJob:
    # Reuse text-safe parsing while keeping this tool's capacity independent.
    base = validate_bigseller_item_id_query_payload(payload, max_sku_count=MAX_SKU_COUNT)
    return BigSellerLinkQueryJob(**vars(base), site=_validate_site(payload.get("site")))


def build_link_query(sku: str, listing_scope: str = "live", page_no: int = 1) -> dict[str, Any]:
    # This is the official Shopee page's "Platform Create Time" sort, NOT BS create_time.
    if listing_scope not in SHOPEE_STATUSES:
        raise ValueError("不支持的商品查询范围")
    # BS's Shopee tabCallback maps the Unlisted tab to case-sensitive "unList".
    # The endpoint ignores a "site" filter; select by returned site, across pages.
    return {**build_listing_query(sku), "orderBy": "platform_create_time", "desc": False,
            "shopeeStatus": SHOPEE_STATUSES[listing_scope], "pageNo": page_no, "pageSize": PAGE_SIZE}


def empty_row(sku: str, message: str, status: str = "failed") -> dict[str, str]:
    return {"sku": sku, "shop_name": "", "product_url": "", "listed_time": "", "item_id": "",
            "listing_scope": "", "status": status, "message": message}


def _listed_time(value: Any) -> datetime:
    try:
        return parse_listing_time(value)
    except BigSellerQueryError:
        raise BigSellerQueryError("商品上架时间缺失或格式异常，无法确认最早上架商品") from None


def safe_product_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2048 or any(character.isspace() or ord(character) < 32 for character in url):
        return ""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443}
                or not any(host == allowed or host == "www." + allowed for allowed in SHOPEE_HOSTS)
                or not parsed.path.strip("/")):
            return ""
    except ValueError:
        return ""
    return url


def _query_listing_scope(session: Any, util: Any, config: dict[str, Any], sku: str,
                         progress: ProgressCallback, listing_scope: str, site: str) -> dict[str, str]:
    # Reuse benchmark's process-wide request gate, business-error classification,
    # bounded retries and server Retry-After handling. No concurrent burst requests.
    seen_pages: set[tuple] = set()
    seen_count = 0
    expected_total = None
    previous_time = None
    first = None
    listed = None
    for page_no in range(1, MAX_PAGES + 1):
        rows, total = request_benchmark_page(
            session, util, config, sku, "views", page_no, progress,
            query=build_link_query(sku, listing_scope, page_no),
        )
        seen_count += len(rows)
        if total is not None:
            if (total < seen_count or (not rows and total > seen_count)
                    or (expected_total is not None and expected_total != total)):
                raise BigSellerQueryError("BigSeller 商品数量与列表结果不一致，无法确认首条商品")
            expected_total = total
        elif expected_total is not None:
            raise BigSellerQueryError("BigSeller 分页缺少商品总数，请重新查询")
        if rows:
            fingerprint = tuple(tuple(str(row.get(key, "")) for key in ("id", "itemId", "shopId", "site")) for row in rows)
            if fingerprint in seen_pages:
                raise BigSellerQueryError("BigSeller 返回重复分页，无法确认所选国家的最早商品")
            seen_pages.add(fingerprint)
        for row in rows:
            current_time = _listed_time(row.get("platformCreateTime"))
            if previous_time is not None and current_time < previous_time:
                raise BigSellerQueryError("BigSeller 未按上架时间升序返回商品，无法确认最早链接")
            previous_time = current_time
            row_site = row.get("site")
            if not isinstance(row_site, str) or row_site.strip().upper() not in SITE_LABELS:
                raise BigSellerQueryError("商品缺少有效国家/地区，无法确认所选国家的最早商品")
            if first is None and row_site.strip().upper() == site:
                first, listed = row, current_time
        if first is not None:
            break
        if (total is not None and seen_count == total) or (total is None and len(rows) < PAGE_SIZE):
            return empty_row(sku, f"当前账号可见的 Shopee {SITE_LABELS[site]}{LISTING_SCOPE_LABELS[listing_scope]}商品中未找到匹配 SKU", "not_found")
    else:
        raise BigSellerQueryError("商品分页超过查询上限，无法确认所选国家的最早商品")
    shop_name = first.get("shopName")
    if (not isinstance(shop_name, str) or not shop_name.strip() or len(shop_name) > 32767
            or ILLEGAL_CHARACTERS_RE.search(shop_name)):
        raise BigSellerQueryError("最早上架商品缺少有效店铺名，未使用后续商品替代")
    item_id = first_item_id([first])
    # BS shopId is an internal ID, not the marketplace shop ID. Use only its link.
    product_url = safe_product_url(first.get("link"))
    return {"sku": sku, "shop_name": shop_name.strip(), "product_url": product_url,
            "listed_time": listed.strftime("%Y-%m-%d %H:%M:%S"), "item_id": item_id,
            "listing_scope": listing_scope, "status": "matched",
            "message": "" if product_url else "最早商品已匹配店铺，但未返回有效商品链接；链接留空"}


def query_first_listing(session: Any, util: Any, config: dict[str, Any], sku: str,
                        progress: ProgressCallback, site: str = "PH") -> dict[str, str]:
    site = _validate_site(site)
    live = _query_listing_scope(session, util, config, sku, progress, "live", site)
    if live["status"] != "not_found":
        return live
    # Only an explicitly successful empty live response permits fallback. Errors,
    # missing first-row fields and invalid sorting must not turn into a new scope.
    progress(f"{sku}：{SITE_LABELS[site]}在售无匹配，继续查询下架商品")
    unlisted = _query_listing_scope(session, util, config, sku, progress, "unlisted", site)
    if unlisted["status"] == "not_found":
        return empty_row(sku, f"当前账号可见的 Shopee {SITE_LABELS[site]}在售及下架商品中均未找到匹配 SKU", "not_found")
    unlisted["message"] = "；".join(filter(None, ("在售无匹配，已取下架商品中最早上架的链接", unlisted["message"])))
    return unlisted


def export_link_results(output_dir: Path, rows: list[dict[str, str]], site: str = "PH") -> Path:
    site = _validate_site(site)
    output_dir.mkdir(parents=True, exist_ok=True)
    incomplete = "_结果不完整" if any(row["status"] == "failed" for row in rows) else ""
    output = output_dir / f"BS抓取链接_{site}{incomplete}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SKU最早上架链接"
    sheet.append([title for _, title, _ in OUTPUT_COLUMNS])
    sheet.freeze_panes = "A2"
    for cell, (_, _, width) in zip(sheet[1], OUTPUT_COLUMNS):
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
        sheet.column_dimensions[cell.column_letter].width = width
    for index, row in enumerate(rows, 2):
        for column, (key, _, _) in enumerate(OUTPUT_COLUMNS, 1):
            if key == "status":
                value = STATUS_LABELS[row[key]]
            elif key == "listing_scope":
                value = LISTING_SCOPE_LABELS.get(row[key], "")
            else:
                value = row[key]
            cell = sheet.cell(index, column, value)
            cell.data_type = "s"
            cell.number_format = "@"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if key == "product_url" and safe_product_url(value):
                cell.hyperlink = value
                cell.style = "Hyperlink"
    sheet.auto_filter.ref = sheet.dimensions
    summary = workbook.create_sheet("查询说明")
    summary.append(["查询规则", QUERY_RULE])
    summary.append(["时间口径", "上架时间为 BS 返回的 Shopee 平台创建时间，不是 BS 内部创建时间。"])
    summary.append(["并列规则", "上架时间相同时保留 BS 返回的第一条商品。"])
    summary.append(["完成状态", "结果不完整" if incomplete else "查询完成"])
    summary.append(["SKU 数", len(rows)])
    for status, label in STATUS_LABELS.items():
        summary.append([label, sum(row["status"] == status for row in rows)])
    for scope, label in LISTING_SCOPE_LABELS.items():
        summary.append([label + "匹配", sum(row["status"] == "matched" and row["listing_scope"] == scope for row in rows)])
    summary.append(["查询国家/地区", f"{SITE_LABELS[site]}（{site}）"])
    summary.column_dimensions["A"].width = 18
    summary.column_dimensions["B"].width = 100
    try:
        workbook.save(output)
    finally:
        workbook.close()
    return output


def run_bigseller_link_query(job: BigSellerLinkQueryJob, progress: ProgressCallback) -> dict[str, Any]:
    site = _validate_site(job.site)
    rows = [empty_row(sku, "尚未执行") for sku in job.skus]
    session = None
    stop_reason = ""
    confirmed = 0
    refreshed = False
    progress(f"准备查询 {len(rows)} 个 SKU；国家/地区：{SITE_LABELS[site]}（{site}）；{QUERY_RULE}")
    try:
        # Create the destination before logging in or sending listing requests.
        job.output_dir.mkdir(parents=True, exist_ok=True)
        config = query_process_config(job)
        config["process_name"] = "BS抓取链接"
        util = load_bigseller_request_util()
        session = login_for_query(util, config, progress)
        for index, sku in enumerate(job.skus):
            try:
                try:
                    row = query_first_listing(session, util, config, sku, progress, site=site)
                except BigSellerAuthenticationError:
                    if refreshed:
                        raise
                    refreshed = True
                    progress("BigSeller 登录失效，重新登录一次后继续当前 SKU")
                    session.close()
                    session = None
                    session = login_for_query(util, config, progress)
                    row = query_first_listing(session, util, config, sku, progress, site=site)
            except (BigSellerAuthenticationError, BigSellerPermissionError, BenchmarkCooldownError) as exc:
                stop_reason = str(exc)
                row = empty_row(sku, stop_reason)
            except BigSellerQueryError as exc:
                row = empty_row(sku, str(exc))
            except Exception:
                row = empty_row(sku, "商品查询异常，请检查网络或联系维护人员")
            rows[index] = row
            confirmed += row["status"] in {"matched", "not_found"}
            progress(f"[BS链接进度 {confirmed}/{len(rows)}] {sku} — {row['shop_name'] or row['message']}")
            if stop_reason:
                break
    except BigSellerQueryError as exc:
        stop_reason = str(exc)
    except Exception:
        stop_reason = "查询初始化失败，请检查 BigSeller 账号、验证码服务、网络及输出目录"
    finally:
        if session is not None:
            session.close()
    if stop_reason:
        for row in rows:
            if row["message"] == "尚未执行":
                row["message"] = "未执行：" + stop_reason
        progress(stop_reason + "；已保留已查询结果，未执行项计入失败")
    counts = {status + "_count": sum(row["status"] == status for row in rows) for status in STATUS_LABELS}
    scope_counts = {scope + "_matched_count": sum(row["status"] == "matched" and row["listing_scope"] == scope for row in rows)
                    for scope in LISTING_SCOPE_LABELS}
    confirmed = counts["matched_count"] + counts["not_found_count"]
    complete = counts["failed_count"] == 0
    message = "全部 SKU 已完成查询" if complete else f"结果不完整：{counts['failed_count']} 个 SKU 查询失败或未执行"
    output_file = ""
    try:
        output_file = str(export_link_results(job.output_dir, rows, site=site))
    except Exception:
        complete = False
        message += "；Excel 导出失败，请检查输出目录权限及磁盘空间后重新运行"
    progress(message)
    if output_file:
        progress(f"Excel 已保存：{output_file}")
    return {"sku_count": len(rows), "site": site, "site_name": SITE_LABELS[site], **counts, **scope_counts, "confirmed_count": confirmed,
            "is_complete": complete, "completion_message": message,
            "rows": rows[:PREVIEW_ROW_LIMIT], "preview_limited": len(rows) > PREVIEW_ROW_LIMIT,
            "failed_skus": [row["sku"] for row in rows if row["status"] == "failed"],
            "output_file": output_file, "output_dir": str(job.output_dir)}
