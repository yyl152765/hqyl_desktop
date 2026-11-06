import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
from openpyxl import load_workbook
from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly import LazadaAdsMonthlyService
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli
from backend.services.lazada_ads_monthly_page import SITE_PROFILES, SpendResult
from backend.services.lazada_ads_monthly_download import DownloadError, parse_coupon_file


class FixturePage:
    calls = []
    failures = {}
    empty = True
    missing_image = False
    rounded = False
    ads = '903.15'
    affiliate = '49.59'

    def __init__(self, cli, store_id, country):
        self.store_id, self.country = store_id, country
        self.currency = SITE_PROFILES[country]['currency']
        self.evidence = {'source':'offline test fixture'}
        self.calls.append(store_id)

    def collect_ads_spend(self, start, end, path):
        failure = self.failures.get(self.store_id)
        if failure:
            raise failure
        if not self.missing_image:
            Image.new('RGB',(80,60),'white').save(path)
        return SpendResult(self.ads, None if self.rounded else Decimal(self.ads), 'rounded_display' if self.rounded else 'exact', self.currency)

    def collect_affiliate_spend(self, start, end, path):
        Image.new('RGB',(80,60),'white').save(path)
        return SpendResult(self.affiliate,Decimal(self.affiliate),'exact',self.currency)

    def prepare_and_search_coupons(self, start, end, path):
        Image.new('RGB',(80,60),'white').save(path)
        return {'is_empty':self.empty,'has_download':not self.empty,'filters_verified':True,'expected_count':0 if self.empty else 8,
                'start_date':start,'end_date':end,'country':self.country,'currency':self.currency}

    def capture_coupon_download_blob(self, path, timeout):
        path = path.with_suffix('.csv')
        values=['2200','29.54','41.70','3305','16525','13.22','20.29','9.91']
        path.write_text('Date,Wallet,Type,Status,Amount\n'+''.join(f'2026-10-02,Coupon,Top up,Success,{v}\n' for v in values),encoding='utf8')
        return path


class BatchIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cli=MagicMock(spec=ZiniaoMonthlyCli)
        self.cli.profile='test-profile'
        self.cli.identity_fingerprint.return_value='test-fingerprint'
        self.cli.list_stores.return_value=[{'store_id':str(i),'store_name':f'Shop-{code}','platform_name':'Lazada','site':code} for i,code in enumerate(SITE_PROFILES,1)]
        self.cli.match_store.side_effect=lambda name,stores: next(s for s in stores if s['store_name']==name)
        self.service=LazadaAdsMonthlyService(self.cli)
        FixturePage.calls,FixturePage.failures=[],{}
        FixturePage.empty,FixturePage.missing_image,FixturePage.rounded=True,False,False
        FixturePage.ads,FixturePage.affiliate='903.15','49.59'
        self.page=patch('backend.services.lazada_ads_monthly.LazadaMonthlyPageAdapter',FixturePage)
        self.page.start();self.addCleanup(self.page.stop)

    def run_batch(self, names=None, **kwargs):
        request={'month':'2026-10','store_names':names or ['Shop-TH'],'output_root':str(self.root)}
        return self.service.run_batch(request,**kwargs)

    def test_all_six_sites_numeric_workbook_and_images(self):
        result=self.run_batch([s['store_name'] for s in self.cli.list_stores.return_value])
        self.assertTrue(result['is_complete'])
        self.assertEqual(len(result['stores']),6)
        wb=load_workbook(result['workbook_path']);self.addCleanup(wb.close)
        self.assertEqual(len(wb.active._images),18)
        for index,row in enumerate(result['stores'],2):
            self.assertEqual(wb.active.cell(index,8).value,853.56)
            self.assertEqual(row['currency'],SITE_PROFILES[row['country']]['currency'])

    def test_file_eight_amounts_and_negative_actual(self):
        FixturePage.empty=False
        result=self.run_batch()
        row=result['stores'][0]
        self.assertEqual(row['coupon_amount'],'22144.66')
        self.assertEqual(row['actual_spend'],'-21291.10')
        self.assertTrue(self.service._healthy(row,Path(result['batch_dir'])))

    def test_missing_evidence_never_success(self):
        FixturePage.missing_image=True
        result=self.run_batch()
        self.assertFalse(result['is_complete'])
        self.assertIsNone(result['stores'][0]['actual_spend'])

    def test_window_limit_stops_before_remaining_shops(self):
        error = ZiniaoCliError('maximum three windows')
        error.code = 'WINDOW_LIMIT'
        self.cli.open_store.side_effect = error
        result = self.run_batch(['Shop-TH', 'Shop-MY'])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.cli.open_store.call_count, 1)
        self.assertEqual(result['stores'][1]['status'], 'pending')
        self.assertFalse(result['is_complete'])

    def test_rounded_amount_leaves_actual_blank(self):
        FixturePage.rounded=True
        result=self.run_batch()
        self.assertFalse(result['is_complete'])
        self.assertIsNone(result['stores'][0]['actual_spend'])
        wb=load_workbook(result['workbook_path']);self.addCleanup(wb.close)
        self.assertIsNone(wb.active.cell(2,8).value)

    def test_auth_failure_stops_remaining_stores(self):
        FixturePage.failures={'3':ZiniaoCliError('auth failure',auth=True)}
        result=self.run_batch(['Shop-TH','Shop-MY'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(FixturePage.calls,['3'])
        self.assertEqual(result['stores'][1]['status'],'pending')

    def test_close_failure_stops_remaining_stores(self):
        self.cli.close_store.side_effect=ZiniaoCliError('close not confirmed')
        result=self.run_batch(['Shop-TH','Shop-MY'])
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(FixturePage.calls,['3'])

    def test_total_failure_is_not_task_success(self):
        FixturePage.failures={'3':ValueError('bad data')}
        result=self.run_batch()
        self.assertFalse(result['ok'])
        self.assertFalse(result['is_complete'])
        self.assertEqual(result['status'],'failed')

    def test_cancel_preserves_all_pending_store_rows(self):
        result=self.run_batch(['Shop-TH','Shop-MY'],is_cancelled=lambda:True)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(len(result['stores']),2)
        self.assertFalse(FixturePage.calls)

    def test_retry_reuses_healthy_merges_and_keeps_run(self):
        FixturePage.failures={'2':ValueError('temporary')}
        result=self.run_batch(['Shop-TH','Shop-MY'])
        FixturePage.failures={}
        retry=self.service.retry_batch(result['manifest_path'])
        self.assertTrue(retry['is_complete'])
        self.assertEqual(retry['run_id'],result['run_id'])
        self.assertEqual(retry['workbook_path'],result['workbook_path'])
        self.assertEqual(FixturePage.calls,['3','2','2'])
        self.assertEqual([r['attempt'] for r in retry['stores']],[1,2])
        self.assertTrue((Path(result['batch_dir'])/'stores'/'MY_2'/'attempt-001'/'result.json').exists())

    def test_tampered_raw_file_forces_whole_store_retry(self):
        FixturePage.empty=False
        result=self.run_batch()
        Path(result['stores'][0]['raw_file']).write_text('tampered')
        retry=self.service.retry_batch(result['manifest_path'])
        self.assertTrue(retry['ok'])
        self.assertEqual(FixturePage.calls,['3','3'])

    def test_tampered_image_forces_retry(self):
        result=self.run_batch()
        Path(result['stores'][0]['evidence_ads']).write_bytes(b'bad')
        retry=self.service.retry_batch(result['manifest_path'])
        self.assertTrue(retry['ok'])
        self.assertEqual(FixturePage.calls,['3','3'])

    def test_changed_account_blocks_retry(self):
        result=self.run_batch()
        self.cli.identity_fingerprint.return_value='different'
        with self.assertRaises(ZiniaoCliError):
            self.service.retry_batch(result['manifest_path'])
        self.assertEqual(FixturePage.calls,['3'])

    def test_changed_store_mapping_blocks_retry(self):
        result=self.run_batch()
        self.cli.list_stores.return_value[2]['store_id']='new-id'
        with self.assertRaises(ValueError):
            self.service.retry_batch(result['manifest_path'])
        self.assertEqual(FixturePage.calls,['3'])

    def test_export_error_persists_manifest_and_retry_recovers(self):
        with patch('backend.services.lazada_ads_monthly.export_ads_monthly_workbook',side_effect=PermissionError('locked')):
            result=self.run_batch()
        self.assertFalse(result['is_complete'])
        self.assertTrue(Path(result['manifest_path']).exists())
        retry=self.service.retry_batch(result['manifest_path'])
        self.assertTrue(retry['ok'])
        self.assertEqual(FixturePage.calls,['3'])

    def test_wrong_country_rejected_before_open(self):
        with self.assertRaises(ValueError):
            self.service.run_batch({'month':'2026-10','country':'MY','store_names':['Shop-TH'],'output_root':str(self.root)})
        self.cli.open_store.assert_not_called()

    def test_incomplete_export_row_count_rejected(self):
        file=self.root/'export.csv'
        file.write_text('Amount\n10\n',encoding='utf8')
        with self.assertRaises(DownloadError):
            parse_coupon_file(file,'2026-10',filters_verified=True,expected_count=2)


if __name__=='__main__':
    unittest.main()
