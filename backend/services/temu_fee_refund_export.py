from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
import copy
import re

from backend.services.temu_on_sale_export import parse_names, write_json, is_temu, batch_lock

KINDS = {'shipping': '发货面单费', 'return_label': '退货面单费', 'refund': '退货退款'}
SCHEMA = 'temu_fee_refund_v1'


def validate_request(payload):
    names = parse_names(payload.get('store_names'))
    supplied = payload.get('store_names')
    lines = supplied.splitlines() if isinstance(supplied, str) else supplied
    if len([line for line in lines if str(line or '').strip()]) != len(names):
        raise ValueError('店铺名称不能重复')
    try:
        start, end = (date.fromisoformat(str(payload.get(k) or '')) for k in ('start_date', 'end_date'))
    except ValueError:
        raise ValueError('请选择有效的开始和结束日期') from None
    if start > end:
        raise ValueError('开始日期不能晚于结束日期')
    return names, start.isoformat(), end.isoformat()


def match_stores(names, catalog):
    """Resolve a complete name first, or a uniquely contained partial name.

    The catalog's complete name is the source of both the exported shop name
    and the operator (the segment before its first hyphen).
    """
    selected, issues, used = [], [], set()
    for name in parse_names(names):
        key = name.casefold()
        exact = [s for s in catalog if str(s.get('store_name') or '').strip().casefold() == key]
        candidates = exact or [s for s in catalog if key in str(s.get('store_name') or '').strip().casefold()]
        candidates = {str(s.get('store_id') or ''): s for s in candidates}
        if len(candidates) != 1 or '' in candidates:
            issues.append({'name': name, 'error': '未匹配到店铺' if not candidates else '匹配到多个店铺，请输入更完整的名称'})
            continue
        store_id, store = next(iter(candidates.items()))
        if not is_temu(store):
            issues.append({'name': name, 'error': '紫鸟平台信息未确认是 TEMU'})
            continue
        if store_id in used:
            issues.append({'name': name, 'error': '多个输入匹配到同一店铺，请删除重复项'})
            continue
        full_name = str(store.get('store_name') or '').strip()
        parts = re.split(r'[-－—–]', full_name, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            issues.append({'name': name, 'error': '完整紫鸟店铺名未包含可识别的运营名和店铺信息'})
            continue
        selected.append({**copy.deepcopy(store), 'operator_name': parts[0].strip()})
        used.add(store_id)
    return {'stores': selected, 'issues': issues, 'can_start': bool(selected) and not issues}


def create_batch(directory, profile, stores, operators, start, end):
    if len(stores) != len(operators):
        raise ValueError('店铺与运营名数量不一致')
    root = Path(directory) / f"TEMU面单及退款_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    (root / 'raw').mkdir()
    (root / 'results').mkdir()
    record = {'schema': SCHEMA, 'run_id': root.name, 'profile': profile, 'status': 'pending',
              'start_date': start, 'end_date': end, 'stores': [
                  {**copy.deepcopy(store), 'operator_name': operator, 'status': 'pending', 'error': '',
                   'exports': {kind: {'status': 'pending'} for kind in KINDS}}
                  for store, operator in zip(stores, operators)]}
    path = root / 'manifest.json'
    write_json(path, record)
    return path


def load_batch(path, profile):
    import json
    path = Path(path).resolve()
    if path.name != 'manifest.json':
        raise ValueError('无效批次清单')
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('schema') != SCHEMA or record.get('profile') != profile:
        raise ValueError('批次无效或与所选紫鸟账号不一致')
    for store in record.get('stores', []):
        for item in store.get('exports', {}).values():
            for field, folder in (('raw_file', 'raw'), ('file', 'results')):
                if item.get(field):
                    candidate = Path(item[field])
                    if candidate.is_absolute() or not (path.parent / candidate).resolve().is_relative_to((path.parent / folder).resolve()):
                        raise ValueError('批次文件路径超出本次导出目录')
    consolidated = record.get('consolidated') or {}
    if consolidated.get('file'):
        candidate = Path(consolidated['file'])
        if candidate.is_absolute() or not (path.parent / candidate).resolve().is_relative_to((path.parent / 'results').resolve()):
            raise ValueError('合并文件路径超出本次导出目录')
    return record


def public_result(path, record):
    stores = copy.deepcopy(record['stores'])
    files = []
    consolidated = copy.deepcopy(record.get('consolidated') or {})
    if consolidated.get('file'):
        consolidated['file'] = str(path.parent / consolidated['file'])
        files.append(consolidated['file'])
    for store in stores:
        for item in store['exports'].values():
            if item.get('file'):
                item['file'] = str(path.parent / item['file'])
                files.append(item['file'])
    complete = record['status'] == 'complete'
    message = ('三类导出及合并总表均已完成' if complete and consolidated.get('file') else
               '三类导出均已完成' if complete else
               '正在逐店导出，请查看各项进度' if record['status'] in {'pending', 'running'} else
               '部分导出失败，已生成部分完成总表，请查看各店铺错误' if consolidated.get('status') == 'partial' else
               record.get('error') or '导出未全部完成，请查看各店铺和各项错误')
    return {**record, 'stores': stores, 'manifest_path': str(path), 'output_dir': str(path.parent),
            'consolidated': consolidated, 'output_files': files, 'is_complete': complete,
            'completion_message': message}


def fail_batch(path, profile, error):
    """Keep completed files visible while ending every pending/running item."""
    path = Path(path).resolve()
    record = load_batch(path, profile)
    record.update(status='failed', error=str(error))
    for store in record['stores']:
        for item in store['exports'].values():
            if item['status'] in {'pending', 'running'}:
                item.update(status='failed', error=str(error))
        failures = [f"{KINDS[k]}：{v.get('error', '')}" for k, v in store['exports'].items() if v['status'] == 'failed']
        store.update(status='failed' if failures else 'success', error='；'.join(failures))
    write_json(path, record)
    return public_result(path, record)


def run_batch(path, gateway, progress):
    path = Path(path).resolve()
    with batch_lock(path):
        record = load_batch(path, gateway.profile)
        if record['status'] != 'pending':
            raise ValueError('该批次已经运行，请创建新批次；已保存结果不会覆盖')
        try:
            return _run_batch(path, gateway, progress, record)
        except Exception as exc:
            return fail_batch(path, gateway.profile, f'批次中断：{exc}')


def _run_batch(path, gateway, progress, record):
    from backend.services.temu_fee_refund_workbook import add_store_columns
    record['status'] = 'running'
    write_json(path, record)
    try:
        gateway.preflight(record['stores'])
    except Exception as exc:
        for store in record['stores']:
            store.update(status='failed', error=str(exc))
            for item in store['exports'].values():
                item.update(status='failed', error=str(exc))
        record['status'] = 'failed'
        write_json(path, record)
        return public_result(path, record)
    for index, store in enumerate(record['stores'], 1):
        store['status'] = 'running'
        write_json(path, record)
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', store['store_name']).strip(' .')[:40] or '店铺'
        for kind, title in KINDS.items():
            item = store['exports'][kind]
            item['status'] = 'running'
            write_json(path, record)
            progress(f"[{index}/{len(record['stores'])}] {store['store_name']}：{title}")
            try:
                if getattr(gateway, 'halted', False):
                    raise ValueError(getattr(gateway, 'halt_reason', '浏览器会话已停止'))
                raw_dir = path.parent / 'raw' / f'{index:03d}_{safe}' / kind
                raw_dir.mkdir(parents=True, exist_ok=False)
                result = gateway.export(store, kind, record['start_date'], record['end_date'], raw_dir)
                if not isinstance(result.get('evidence'), dict) or not result['evidence']:
                    raise ValueError('缺少本次导出任务的确认信息')
                item['evidence'] = copy.deepcopy(result['evidence'])
                if result.get('no_data'):
                    item.update(status='no_data', row_count=0)
                else:
                    source = Path(result.get('path') or '').resolve()
                    if not source.is_file() or not source.is_relative_to(raw_dir.resolve()):
                        raise ValueError('下载文件不在本次导出目录内')
                    item['raw_file'] = str(source.relative_to(path.parent))
                    expected_count = result.get('row_count')
                    if type(expected_count) is not int or expected_count < 0:
                        raise ValueError('缺少有效的下载数据行数校验')
                    item['raw_row_count'] = expected_count
                    destination = path.parent / 'results' / f'{index:03d}_{safe}_{title}{source.suffix.lower()}'
                    staging = destination.with_name(f'.{destination.stem}.{uuid4().hex}.pending{destination.suffix}')
                    try:
                        count = add_store_columns(source, staging, store['operator_name'], store['store_name'])
                        if count != expected_count:
                            raise ValueError(f'输出数据行数 {count} 与下载校验行数 {expected_count} 不一致')
                        staging.replace(destination)
                    finally:
                        staging.unlink(missing_ok=True)
                    item.update(status='success', raw_file=str(source.relative_to(path.parent)),
                                file=str(destination.relative_to(path.parent)), row_count=count)
            except Exception as exc:
                item.update(status='failed', error=str(exc))
                progress(f"{store['store_name']} {title}失败：{exc}")
            write_json(path, record)
        failures = [f"{KINDS[k]}：{v.get('error', '')}" for k, v in store['exports'].items() if v['status'] == 'failed']
        store.update(status='failed' if failures else 'success', error='；'.join(failures))
        write_json(path, record)
    record['status'] = 'complete' if all(s['status'] == 'success' for s in record['stores']) else 'failed'
    if any(item['status'] in {'success', 'no_data'} for store in record['stores'] for item in store['exports'].values()):
        from backend.services.temu_fee_refund_consolidated import create_consolidated_workbook
        try:
            record['consolidated'] = create_consolidated_workbook(path, record)
        except Exception as exc:
            record['consolidated'] = {'status': 'failed', 'error': str(exc)}
            record.update(status='failed', error=f'合并总表失败：{exc}')
    write_json(path, record)
    return public_result(path, record)
