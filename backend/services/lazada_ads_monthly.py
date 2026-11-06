"""Lazada Ads Monthly Report batch service.

Orchestrates multi-store serial collection, CLI interaction, page adapters,
coupon file downloading/parsing, Decimal spend formula calculation,
evidence retention, manifest auditing, and Excel workbook delivery.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli, normalize_store_name
from backend.services.lazada_ads_monthly_download import (
    compute_sha256,
    DownloadError,
    ParseAudit,
    parse_coupon_file,
    snapshot_directory,
    wait_for_download,
)
from backend.services.lazada_ads_monthly_page import (
    LazadaMonthlyPageAdapter,
    SpendResult,
    detect_country_from_url_or_name,
    get_site_profile,
)
from backend.services.lazada_ads_monthly_workbook import export_ads_monthly_workbook

logger = logging.getLogger(__name__)

SUPPORTED_COUNTRIES = ("AUTO", "PH", "MY", "TH", "ID", "VN", "SG")
FORMULA_VERSION = "B-D-F_v1"
SCHEMA_VERSION = "lazada_ads_monthly_v2"


def get_default_month() -> str:
    """Return previous complete calendar month (YYYY-MM)."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def calculate_month_dates(month_str: str) -> tuple[str, str]:
    """Validate month YYYY-MM and return start_date, end_date (YYYY-MM-01, YYYY-MM-DD).

    Per spec 1.3:
    Rejects future months, current unended month, or invalid formats.
    """
    if not isinstance(month_str, str) or not re.match(r"^\d{4}-\d{2}$", month_str.strip()):
        raise ValueError(f"月份格式必须为 YYYY-MM，收到: '{month_str}'")

    year_str, m_str = month_str.strip().split("-")
    year, month = int(year_str), int(m_str)
    if month < 1 or month > 12:
        raise ValueError(f"月份必须在 01 至 12 之间，收到: {month}")

    date(year, month, 1)
    today = date.today()
    if year > today.year or (year == today.year and month >= today.month):
        raise ValueError(f"所选月份 {month_str} 尚未完整结束，仅支持历史完整自然月")

    _, last_day = calendar.monthrange(year, month)
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{last_day:02d}"
    return start_date, end_date


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip('. ')[:100]
    if cleaned in ('', '.', '..') or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', cleaned, re.I):
        cleaned = '_' + cleaned.replace('.', '_')
    return cleaned


def default_output_root() -> Path:
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        desktop = Path.home()
    return desktop / "log"


def validate_ads_monthly_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate incoming request parameters."""
    if not isinstance(payload, dict):
        raise ValueError('请求必须是对象')
    month = str(payload.get("month") or "").strip() or get_default_month()
    start_date, end_date = calculate_month_dates(month)

    country = str(payload.get("country") or "AUTO").strip().upper()
    if country not in SUPPORTED_COUNTRIES:
        raise ValueError(f"不支持的站点选项: {country}，支持: {', '.join(SUPPORTED_COUNTRIES)}")

    # Extract store names
    raw_stores = payload.get("store_names") or []
    if isinstance(raw_stores, str):
        lines = raw_stores.splitlines()
    elif isinstance(raw_stores, list) and all(isinstance(s, str) for s in raw_stores):
        lines = raw_stores
    else:
        raise ValueError('店铺名称必须是逐行文本或字符串数组')

    store_names: list[str] = []
    seen = set()
    for s in lines:
        s = normalize_store_name(s)
        if s and s not in seen:
            seen.add(s)
            store_names.append(s)

    if not store_names:
        raise ValueError("请至少提供一个要采集的店铺名称")

    if len(store_names) > 200:
        raise ValueError("单批次店铺数量上限为 200 家，请分批次执行")

    cli_profile = str(payload.get("cli_profile") or "").strip()

    output_root_raw = payload.get("output_root")
    output_root = Path(output_root_raw).resolve() if output_root_raw else default_output_root()

    return {
        "schema": SCHEMA_VERSION,
        "month": month,
        "start_date": start_date,
        "end_date": end_date,
        "country": country,
        "store_names": store_names,
        "cli_profile": cli_profile,
        "output_root": str(output_root),
    }


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def image_valid(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


_BATCH_LOCK = threading.Lock()


class LazadaAdsMonthlyService:
    def __init__(self, cli: ZiniaoMonthlyCli | None = None):
        self.cli = cli

    def _resolve(self, cli, request):
        stores = cli.list_stores()
        resolved, issues, ids = [], [], set()
        for name in request['store_names']:
            try:
                st = cli.match_store(name, stores)
                if 'lazada' not in st.get('platform_name','').lower():
                    raise ValueError('目标不是 Lazada 店铺')
                site = st.get('site','').upper() or detect_country_from_url_or_name('', st['store_name'])
                get_site_profile(site)
                if request['country'] != 'AUTO' and request['country'] != site:
                    raise ValueError('指定国家与店铺识别的站点不一致')
                if st['store_id'] in ids:
                    raise ValueError('多个输入对应同一个店铺，请合并后重试')
                ids.add(st['store_id'])
                resolved.append({'requested_name':name,'store_id':st['store_id'], 'store_name':st['store_name'],
                                 'platform':st['platform_name'],'country':site})
            except (ValueError, ZiniaoCliError) as exc:
                issues.append({'requested_name':name,'error':str(exc)})
        return resolved, issues

    def preview_stores(self, request):
        request = validate_ads_monthly_request(request)
        cli = self.cli or ZiniaoMonthlyCli(profile=request['cli_profile'])
        cli.ready()
        resolved, issues = self._resolve(cli, request)
        return {'ok':not issues,'matched_count':len(resolved),'matched':resolved,'issues':issues,'profile':cli.profile}

    def run_batch(self, request, progress_cb=None, is_cancelled=None):
        if not _BATCH_LOCK.acquire(blocking=False):
            raise ValueError('已有 Lazada 广告月报任务正在运行')
        try:
            request = validate_ads_monthly_request(request)
            cli = self.cli or ZiniaoMonthlyCli(profile=request['cli_profile'])
            cli.ready()
            stores, issues = self._resolve(cli, request)
            if issues:
                raise ValueError('; '.join(f'{r["requested_name"]}: {r["error"]}' for r in issues))
            run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:6]
            batch_dir = Path(request['output_root']) / 'Lazada广告月报' / request['month'] / run_id
            batch_dir.mkdir(parents=True, exist_ok=False)
            manifest = {'schema_version':SCHEMA_VERSION,'formula_version':FORMULA_VERSION,'run_id':run_id,
                        'created_at':datetime.now().isoformat(),'month':request['month'],
                        'start_date':request['start_date'],'end_date':request['end_date'],
                        'country_scope':request['country'],'cli_profile':cli.profile,
                        'account_fingerprint':cli.identity_fingerprint(), 'status':'pending',
                        'stores':[{**s,'month':request['month'],'currency':get_site_profile(s['country'])['currency'],
                                   'run_id':run_id,'status':'pending','attempt':0} for s in stores]}
            atomic_json(batch_dir/'manifest.json', manifest)
            return self._execute(cli, batch_dir, manifest, progress_cb, is_cancelled)
        finally:
            _BATCH_LOCK.release()

    def _healthy(self, row, batch_dir):
        try:
            if row['status'] != 'success' or row['currency'] != get_site_profile(row['country'])['currency']:
                return False
            values = [Decimal(row[k]) for k in ('ads_spend','affiliate_spend','coupon_amount','actual_spend')]
            if not all(v.is_finite() for v in values) or values[0]-values[1]-values[2] != values[3]:
                return False
            hashes = row['artifacts']
            if not hashes or not row.get('metrics_path') or not row.get('parse_audit_path'):
                return False
            for value in ('evidence_ads','evidence_affiliate','evidence_coupon','metrics_path','parse_audit_path'):
                if row[value] not in hashes:
                    return False
            if row.get('raw_file') and row['raw_file'] not in hashes:
                return False
            for filename, checksum in hashes.items():
                path = Path(filename).resolve()
                if not path.is_relative_to(batch_dir.resolve()) or compute_sha256(path) != checksum:
                    return False
            if not all(image_valid(Path(row[k])) for k in ('evidence_ads','evidence_affiliate','evidence_coupon')):
                return False
            audit = json.loads(Path(row['parse_audit_path']).read_text(encoding='utf8'))
            if Decimal(audit['total_amount']) != values[2]:
                return False
            if not row.get('raw_file') and not (audit.get('verified_empty') is True and values[2] == 0):
                return False
            return True
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            return False

    def _execute(self, cli, batch_dir, manifest, progress_cb, is_cancelled):
        (batch_dir/'logs').mkdir(exist_ok=True)
        def event(kind, **data):
            record = {'timestamp':datetime.now().isoformat(),'type':kind,**data}
            with (batch_dir/'logs'/'events.jsonl').open('a',encoding='utf8') as f:
                f.write(json.dumps(record,ensure_ascii=False)+'\n')
            if progress_cb:
                progress_cb(record)
        def cancelled():
            return bool(is_cancelled and is_cancelled())
        def checkpoint():
            if cancelled():
                raise DownloadError('用户取消任务',code='cancelled')
        manifest_path = batch_dir/'manifest.json'
        manifest['status'] = 'running'
        manifest.pop('error', None)
        atomic_json(manifest_path, manifest)
        event('batch_start',run_id=manifest['run_id'],month=manifest['month'],stores_count=len(manifest['stores']))
        for idx, row in enumerate(manifest['stores'],1):
            if self._healthy(row,batch_dir):
                event('store_reused',store_name=row['store_name'])
                continue
            if cancelled():
                manifest['status']='cancelled'
                break
            row.update(status='running',attempt=int(row.get('attempt',0))+1, note='',actual_spend=None,
                       ads_spend=None,affiliate_spend=None,coupon_amount=None,
                       evidence_ads=None,evidence_affiliate=None,evidence_coupon=None,raw_file=None,
                       artifacts={},captured_at=datetime.now().isoformat())
            attempt = batch_dir/'stores'/f'{row["country"]}_{sanitize_filename(row["store_id"])}'/f'attempt-{row["attempt"]:03d}'
            (attempt/'evidence').mkdir(parents=True,exist_ok=False)
            (attempt/'raw').mkdir()
            event('store_start',index=idx,total=len(manifest['stores']),store_name=row['store_name'])
            atomic_json(manifest_path,manifest)
            stop = False
            try:
                opened = cli.open_store(row['store_id'],row['store_name'],entry_url=f'https://{get_site_profile(row["country"])["host"]}/sponsor/solutions/ads/report/')
                if isinstance(opened,dict):
                    row['window_reused'] = opened.get('reused')
                    atomic_json(manifest_path,manifest)
                adapter = LazadaMonthlyPageAdapter(cli,row['store_id'],row['country'])
                for kind,method,key in (('ads','collect_ads_spend','ads_spend'),('affiliate','collect_affiliate_spend','affiliate_spend')):
                    checkpoint()
                    event('store_step',store_name=row['store_name'],step=key)
                    screenshot = attempt/'evidence'/f'{kind}.png'
                    result = getattr(adapter,method)(manifest['start_date'],manifest['end_date'],screenshot)
                    if result.currency != row['currency']:
                        raise ValueError('金额币种与站点不一致')
                    if result.decimal_value is not None and result.decimal_value.is_finite() and result.precision == 'exact':
                        row[key] = str(result.decimal_value)
                    row['evidence_'+kind] = str(screenshot)
                checkpoint()
                event('store_step',store_name=row['store_name'],step='coupon_history')
                screenshot = attempt/'evidence'/'coupons.png'
                filters = adapter.prepare_and_search_coupons(manifest['start_date'],manifest['end_date'],screenshot)
                row['evidence_coupon'] = str(screenshot)
                if filters.get('filters_verified') is not True or filters.get('expected_count') is None:
                    raise ValueError('优惠券筛选或完整记录数未核实')
                if filters.get('start_date') != manifest['start_date'] or filters.get('end_date') != manifest['end_date'] or filters.get('country') != row['country'] or filters.get('currency') != row['currency']:
                    raise ValueError('优惠券日期/国家/币种不一致')
                if filters.get('is_empty') is True and filters['expected_count'] == 0:
                    row['coupon_amount']='0'
                    audit={'verified_empty':True,'total_amount':'0','filters':filters}
                elif filters.get('has_download'):
                    checkpoint()
                    raw = adapter.capture_coupon_download_blob(attempt/'raw'/'coupons.xlsx',timeout=45)
                    if not isinstance(raw, Path) or not raw.resolve().is_relative_to((attempt/'raw').resolve()):
                        raise DownloadError('导出文件未返回可核实的本批次路径')
                    amount, parsed = parse_coupon_file(raw,manifest['month'],filters_verified=True,expected_count=filters['expected_count'],currency=row['currency'])
                    row['coupon_amount'],row['raw_file']=str(amount),str(raw)
                    audit=parsed.to_dict()
                else:
                    raise DownloadError('优惠券记录存在，但没有可用导出入口')
                checkpoint()
                row['parse_audit_path']=str(attempt/'parse_audit.json')
                atomic_json(Path(row['parse_audit_path']),audit)
                row['metrics_path']=str(attempt/'metrics.json')
                atomic_json(Path(row['metrics_path']),adapter.evidence)
                for key in ('evidence_ads','evidence_affiliate','evidence_coupon'):
                    if not image_valid(Path(row[key])):
                        raise ValueError('原始截图缺失或损坏，无法完成该店报表')
                for filename in (row['evidence_ads'],row['evidence_affiliate'],row['evidence_coupon'],row['metrics_path'],row['parse_audit_path'],row['raw_file']):
                    if filename:
                        row['artifacts'][filename]=compute_sha256(Path(filename))
                if all(row[k] is not None for k in ('ads_spend','affiliate_spend','coupon_amount')):
                    values=[Decimal(row[k]) for k in ('ads_spend','affiliate_spend','coupon_amount')]
                    result=values[0]-values[1]-values[2]
                    if any(len(v.as_tuple().digits)>15 for v in [*values,result]):
                        for key in ('ads_spend','affiliate_spend','coupon_amount'):
                            if len(Decimal(row[key]).as_tuple().digits)>15:
                                row[key]=None
                        raise ValueError('金额超过 Excel 15 位精度，需人工复核')
                    row['actual_spend'],row['status']=str(result),'success'
                else:
                    row['status'],row['note']='partial','precision_unverified：广告或联盟金额未精确获取'
            except Exception as exc:
                row['status']='cancelled' if isinstance(exc,DownloadError) and exc.code=='cancelled' else 'failed'
                row['note'],row['actual_spend']=str(exc),None
                if row['status']=='cancelled':
                    manifest['status'],stop='cancelled',True
                if isinstance(exc,ZiniaoCliError) and (exc.auth or getattr(exc,'code','') in ('WINDOW_LIMIT','WINDOW_STATE_UNVERIFIED')):
                    manifest['status'],stop='blocked',True
            finally:
                try:
                    cli.close_store(row['store_id'])
                except Exception as exc:
                    row['status'],row['actual_spend']='failed',None
                    row['note'] += '; 窗口关闭未确认，已停止批次: '+str(exc)
                    manifest['status'],stop='blocked',True
                atomic_json(attempt/'result.json',row)
                atomic_json(manifest_path,manifest)
                event('store_finish',store_name=row['store_name'],status=row['status'],note=row['note'])
            if stop:
                break
        if manifest['status'] == 'running':
            statuses = [r['status'] for r in manifest['stores']]
            manifest['status']='success' if all(s=='success' for s in statuses) else ('partial' if 'success' in statuses else 'failed')
        output = batch_dir/f'广告报表_{manifest["month"]}_{manifest["run_id"]}.xlsx'
        try:
            export_ads_monthly_workbook(output,manifest['stores'],run_meta=manifest)
            manifest['workbook_path']=str(output)
        except Exception as exc:
            manifest['status'],manifest['error']='failed',str(exc)
            manifest['workbook_path']=''
        manifest['updated_at']=datetime.now().isoformat()
        atomic_json(manifest_path,manifest)
        event('batch_finish',status=manifest['status'],manifest=str(manifest_path))
        complete=manifest['status']=='success'
        return {'ok':complete,'is_complete':complete,'completion_message':manifest.get('error') or '仍有店铺未完成，请查看说明并重试',
                'run_id':manifest['run_id'],'status':manifest['status'],'batch_dir':str(batch_dir),
                'workbook_path':manifest['workbook_path'],'manifest_path':str(manifest_path),'stores':manifest['stores']}

    def retry_batch(self, manifest_file, progress_cb=None, is_cancelled=None):
        if not _BATCH_LOCK.acquire(blocking=False):
            raise ValueError('已有 Lazada 广告月报任务正在运行')
        try:
            path=Path(manifest_file).resolve()
            manifest=json.loads(path.read_text(encoding='utf8'))
            if manifest.get('schema_version')!=SCHEMA_VERSION or manifest.get('formula_version')!=FORMULA_VERSION:
                raise ValueError('批次结构或公式版本不一致，请新建批次重新采集')
            if not manifest.get('stores') or len({s['store_id'] for s in manifest['stores']}) != len(manifest['stores']):
                raise ValueError('批次店铺清单缺失或重复')
            request=validate_ads_monthly_request({'month':manifest['month'],'country':manifest['country_scope'],
                                                'store_names':[s['requested_name'] for s in manifest['stores']]})
            if (request['start_date'],request['end_date']) != (manifest['start_date'],manifest['end_date']):
                raise ValueError('批次日期范围已变化')
            cli=self.cli or ZiniaoMonthlyCli(profile=manifest['cli_profile'])
            if cli.profile != manifest['cli_profile'] or cli.identity_fingerprint() != manifest['account_fingerprint']:
                raise ZiniaoCliError('重试账号与原批次不一致，请恢复原账号',auth=True)
            cli.ready()
            resolved,issues=self._resolve(cli,request)
            original=[{k:s[k] for k in ('requested_name','store_id','store_name','platform','country')} for s in manifest['stores']]
            if issues or resolved!=original:
                raise ValueError('店铺映射或国家已变化，请新建批次重新预检')
            for row in manifest['stores']:
                if row.get('month') != manifest['month'] or row.get('run_id') != manifest['run_id']:
                    raise ValueError('店铺记录与原批次身份不一致')
            return self._execute(cli,path.parent,manifest,progress_cb,is_cancelled)
        finally:
            _BATCH_LOCK.release()
