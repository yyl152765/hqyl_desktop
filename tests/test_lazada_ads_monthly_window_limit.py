"""Running-window admission and acknowledged-but-incomplete close regressions."""
import unittest
from unittest.mock import patch

from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli


def active(*ids):
    return {'items': [{'storeId': sid} for sid in ids], 'total': len(ids)}


class MonthlyWindowLimitTests(unittest.TestCase):
    def setUp(self):
        self.cli = ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        self.cli.targets = {}
        self.cli.owned_windows = set()
        self.cli.expected_urls = {}
        self.cli.download_dirs = {}
        sleep = patch('backend.services.lazada_ads_monthly_cli.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_fourth_window_is_not_opened(self):
        with patch.object(self.cli, 'run', return_value=active('one', 'two', 'three')) as call:
            with self.assertRaises(ZiniaoCliError) as raised:
                self.cli.open_store('four', 'Fourth shop')
        self.assertEqual(raised.exception.code, 'WINDOW_LIMIT')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0][:3], ['zclaw', 'invoke', 'extract_data'])

    def test_existing_third_window_can_be_reused_without_being_closed(self):
        with patch.object(self.cli, 'run', side_effect=[active('one', 'two', 'three'),
                {'storeId': 'three', 'storeName': 'Third shop', 'reused': True},
                active('one', 'two', 'three'), {'targetId': 'page'}]) as call:
            self.assertTrue(self.cli.open_store('three', 'Third shop')['reused'])
            self.cli.close_store('three')
        self.assertEqual(call.call_count, 4)
        self.assertEqual(self.cli.owned_windows, set())

    def test_new_third_window_is_owned_and_closed_before_next_shop(self):
        with patch.object(self.cli, 'run', side_effect=[active('one', 'two'),
                {'storeId': 'three', 'storeName': 'Third shop', 'reused': False},
                active('one', 'two', 'three'), {'targetId': 'page'},
                {'sent': True}, active('one', 'two')]):
            self.cli.open_store('three', 'Third shop')
            self.assertIn('three', self.cli.owned_windows)
            self.cli.close_store('three')
        self.assertNotIn('three', self.cli.owned_windows)

    def test_unverified_inventory_prevents_open(self):
        for data in ({}, active('one', 'one'), {'items': [], 'total': 1},
                     {'items': [{}], 'total': 1}, {'items': [], 'total': False}):
            with self.subTest(data=data), patch.object(self.cli, 'run', return_value=data) as call:
                with self.assertRaises(ZiniaoCliError) as raised:
                    self.cli.open_store('new', 'New shop')
                self.assertEqual(raised.exception.code, 'WINDOW_STATE_UNVERIFIED')
                self.assertEqual(call.call_count, 1)

    def test_concurrent_external_open_preserves_owned_window_for_cleanup(self):
        with patch.object(self.cli, 'run', side_effect=[active('one', 'two'),
                {'storeId': 'three', 'storeName': 'Third shop', 'reused': False},
                active('one', 'two', 'three', 'external')]):
            with self.assertRaises(ZiniaoCliError) as raised:
                self.cli.open_store('three', 'Third shop')
        self.assertEqual(raised.exception.code, 'WINDOW_LIMIT')
        self.assertIn('three', self.cli.owned_windows)

    def test_close_ack_without_disappearance_retains_ownership(self):
        self.cli.owned_windows.add('one')
        self.cli.targets['one'] = 'page'
        with patch.object(self.cli, 'run', side_effect=[{'sent': True}] + [active('one')]*6) as call:
            with self.assertRaises(ZiniaoCliError):
                self.cli.close_store('one')
        self.assertIn('one', self.cli.owned_windows)
        self.assertEqual(self.cli.targets['one'], 'page')
        self.assertEqual(sum(c.args[0][:2] == ['store', 'close'] for c in call.call_args_list), 1)

    def test_delayed_close_is_polled_without_resending_close(self):
        self.cli.owned_windows.add('one')
        with patch.object(self.cli, 'run', side_effect=[{'sent': True}, active('one'), active()]) as call:
            self.cli.close_store('one')
        self.assertNotIn('one', self.cli.owned_windows)
        self.assertEqual(sum(c.args[0][:2] == ['store', 'close'] for c in call.call_args_list), 1)


if __name__ == '__main__':
    unittest.main()
