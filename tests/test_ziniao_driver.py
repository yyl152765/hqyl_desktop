"""Driver repairs use a verified candidate and never destroy the previous file."""
from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from backend.core import ziniao_driver as module


def pe_bytes():
    value = bytearray(512)
    value[:2] = b'MZ'
    struct.pack_into('<I', value, 60, 64)
    value[64:68] = b'PE\0\0'
    struct.pack_into('<H', value, 68, 0x8664)
    return bytes(value)


class Response:
    def __init__(self, data, *, status=200, headers=None, stream_error=False):
        self.data, self.status_code = data, status
        self.headers = {'Content-Length': str(len(data))} if headers is None else headers
        self.stream_error = stream_error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        yield self.data[:10]
        if self.stream_error:
            raise requests.ConnectionError('private-network-canary')
        yield self.data[10:]


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class DriverRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='private-path-canary-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.target = self.folder / 'chromedriver138.exe'
        self.browser = SimpleNamespace(driver_folder_path=str(self.folder))
        self.opened = {'core_type': 'Chromium', 'core_version': '138.0.7204.50'}
        self.bytes = pe_bytes()
        self.download_url = 'https://cdn-superbrowser-attachment.ziniao.com/webdriver/exe_64/chromedriver138.exe'
        self.entry = {'name': 'chromedriver138', 'url': self.download_url,
                      'sha1': hashlib.sha1(self.bytes).hexdigest()}
        self.network = Session([self.config(), Response(self.bytes)])
        self.network_patch = patch.object(module.requests, 'Session', return_value=self.network)
        self.session_factory = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.process_patch = patch.object(module.subprocess, 'run', side_effect=self.version)
        self.process = self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def config(self, value=None):
        return Response(json.dumps([self.entry] if value is None else value).encode())

    @staticmethod
    def version(args, **kwargs):
        kwargs['stdout'].write(b'ChromeDriver 138.0.7204.50 (fixture)\r\n')
        return subprocess.CompletedProcess(args, 0)

    def ensure(self):
        return module.ensure_ziniao_driver(self.browser, self.opened)

    def leftovers(self):
        return [x.name for x in self.folder.iterdir() if '.download.exe' in x.name]

    def test_good_cached_driver_works_offline(self):
        self.target.write_bytes(self.bytes)
        self.assertEqual(self.ensure(), self.target)
        self.session_factory.assert_not_called()
        self.assertEqual(self.process.call_args.args[0], [str(self.target), '--version'])
        self.assertEqual(self.process.call_args.kwargs['timeout'], 5)

    def test_missing_driver_downloads_only_matching_official_file(self):
        messages = []
        actual = module.ensure_ziniao_driver(self.browser, self.opened, messages.append)
        self.assertEqual(actual.read_bytes(), self.bytes)
        self.assertEqual([c[0] for c in self.network.calls], [module.CONFIG_URL, self.download_url])
        self.assertTrue(all(c[1]['allow_redirects'] is False for c in self.network.calls))
        self.assertFalse(self.network.trust_env)
        self.assertTrue(self.network.closed)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(len(messages), 2)
        self.assertEqual(list(self.folder.glob('*.bak')), [])

    def test_truncated_file_is_not_executed_and_is_backed_up(self):
        self.target.write_bytes(b'MZbad')
        self.assertEqual(self.ensure().read_bytes(), self.bytes)
        self.assertEqual(self.process.call_count, 1)  # Candidate only.
        backup, = self.folder.glob('*.bak')
        self.assertEqual(backup.read_bytes(), b'MZbad')
        self.assertEqual(self.leftovers(), [])

    def test_other_version_is_repaired_without_touching_other_driver(self):
        self.target.write_bytes(self.bytes)
        other = self.folder / 'chromedriver142.exe'
        other.write_bytes(b'other-driver')
        def version(args, **kwargs):
            if Path(args[0]) == self.target:
                kwargs['stdout'].write(b'ChromeDriver 137.0.1.2\n')
                return subprocess.CompletedProcess(args, 0)
            return self.version(args, **kwargs)
        self.process.side_effect = version
        self.ensure()
        self.assertEqual(other.read_bytes(), b'other-driver')
        self.assertEqual(len(list(self.folder.glob('*.bak'))), 1)

    def test_interrupted_download_leaves_old_file_untouched_and_no_candidate(self):
        self.target.write_bytes(b'old-invalid-driver')
        self.network.responses = [self.config(), Response(self.bytes, stream_error=True)]
        with self.assertRaises(module.ZiniaoDriverError) as caught:
            self.ensure()
        self.assertNotIn('private-', str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b'old-invalid-driver')
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(list(self.folder.glob('*.bak')), [])
        self.process.assert_not_called()

    def test_hash_mismatch_is_never_executed(self):
        self.network.responses = [self.config(), Response(self.bytes + b'changed')]
        with self.assertRaisesRegex(module.ZiniaoDriverError, '完整性校验失败'):
            self.ensure()
        self.assertFalse(self.target.exists())
        self.process.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_hash_valid_but_bad_pe_is_never_executed(self):
        bad = b'<html>error</html>'
        self.entry['sha1'] = hashlib.sha1(bad).hexdigest()
        self.network.responses = [self.config(), Response(bad)]
        with self.assertRaisesRegex(module.ZiniaoDriverError, '执行或版本校验'):
            self.ensure()
        self.process.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_candidate_wrong_major_or_nonzero_exit_never_replaces_old(self):
        self.target.write_bytes(b'old')
        for output, code in [(b'ChromeDriver 137.0.1.2', 0), (b'ChromeDriver 138.0.1.2', 1),
                             (b'private-output', 0), (b'x' * 9000, 0)]:
            with self.subTest(code=code, output=output[:25]):
                self.network.responses = [self.config(), Response(self.bytes)]
                def run(args, **kwargs):
                    kwargs['stdout'].write(output)
                    return subprocess.CompletedProcess(args, code)
                self.process.side_effect = run
                with self.assertRaises(module.ZiniaoDriverError) as caught:
                    self.ensure()
                self.assertNotIn('private-output', str(caught.exception))
                self.assertEqual(self.target.read_bytes(), b'old')
                self.assertEqual(self.leftovers(), [])

    def test_manifest_rejects_unsafe_urls_and_ambiguous_entries(self):
        invalid_urls = ['http://cdn-superbrowser-attachment.ziniao.com/webdriver/exe_64/chromedriver138.exe',
                        'https://evil.example/driver.exe', self.download_url + '?secret=private-canary',
                        self.download_url.replace('138', '137'), self.download_url.replace('.com/', '.com:443/'),
                        self.download_url.replace('/webdriver/', '/webdriver/../webdriver/'),
                        self.download_url.replace('https://', 'https://user:password@')]
        entries = [[dict(self.entry, url=url)] for url in invalid_urls]
        entries += [[self.entry, self.entry], [], {}, [{'name': 'chromedriver138'}],
                    [dict(self.entry, sha1='invalid')]]
        for value in entries:
            with self.subTest(value=value):
                self.network.responses = [self.config(value)]
                self.network.calls = []
                with self.assertRaises(module.ZiniaoDriverError):
                    self.ensure()
                self.assertEqual(len(self.network.calls), 1)
                self.assertFalse(self.target.exists())
                self.assertEqual(self.leftovers(), [])

    def test_rejects_redirect_invalid_length_and_incomplete_stream(self):
        bad_responses = [Response(b'{}', status=302), Response(b'{}', status=500),
                         Response(b'{}', headers={'Content-Length': '4'}),
                         Response(b'{}', headers={'Content-Length': '-2'}),
                         Response(b'{}', headers={'Content-Length': str(1024 * 1024)}),
                         Response(b'{}', headers={'Content-Encoding': 'gzip'}),
                         Response(b'not json'), Response(b'')]
        for response in bad_responses:
            with self.subTest(headers=response.headers, status=response.status_code):
                self.network.responses = [response]
                with self.assertRaises(module.ZiniaoDriverError):
                    self.ensure()
                self.assertFalse(self.target.exists())
                self.assertEqual(self.leftovers(), [])
                self.assertTrue(response.closed)

    def test_size_and_time_limits_discard_partial_candidate(self):
        for limit in (50,):
            self.network.responses = [self.config(), Response(self.bytes, headers={})]
            with patch.object(module, '_MAX_DRIVER_BYTES', limit):
                with self.assertRaisesRegex(module.ZiniaoDriverError, '大小限制'):
                    self.ensure()
            self.assertEqual(self.leftovers(), [])
        with patch.object(module.time, 'monotonic', side_effect=[0, 100]):
            with self.assertRaisesRegex(module.ZiniaoDriverError, '超时'):
                self.ensure()
        self.assertEqual(self.leftovers(), [])

    def test_untrusted_core_version_or_type_causes_no_network_or_exec(self):
        for version in ('../private-path', '', None, '0', '0138.0.1', '138?password=x', '138.1.2.3.4'):
            with self.subTest(version=version):
                self.opened['core_version'] = version
                with self.assertRaises(module.ZiniaoDriverError):
                    self.ensure()
        self.opened['core_version'] = '138'
        for core in (None, False, 'Firefox', '0'):
            self.opened['core_type'] = core
            with self.assertRaises(module.ZiniaoDriverError):
                self.ensure()
        self.session_factory.assert_not_called()
        self.process.assert_not_called()

    def test_cached_access_denied_keeps_file_and_does_not_download(self):
        self.target.write_bytes(self.bytes)
        self.process.side_effect = PermissionError(errno.EACCES, 'private-error', 'private-path')
        with self.assertRaises(module.ZiniaoDriverError) as caught:
            self.ensure()
        self.assertIn('errno 13', str(caught.exception))
        self.assertNotIn('private-', str(caught.exception))
        self.session_factory.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.bytes)

    def test_cached_winerror193_is_repaired_once(self):
        self.target.write_bytes(self.bytes)
        error = OSError('private-error'); error.winerror = 193
        count = 0
        def run(args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise error
            return self.version(args, **kwargs)
        self.process.side_effect = run
        self.ensure()
        self.assertEqual(count, 2)
        self.assertEqual(len(self.network.calls), 2)

    def test_candidate_timeout_is_redacted_and_keeps_old_file(self):
        self.target.write_bytes(b'old')
        self.process.side_effect = subprocess.TimeoutExpired('private-path-canary', 5)
        with self.assertRaises(module.ZiniaoDriverError) as caught:
            self.ensure()
        self.assertNotIn('private-', str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(self.leftovers(), [])

    def test_candidate_architecture_failure_keeps_native_code_without_private_text(self):
        self.target.write_bytes(b'old')
        error = OSError('private-path-and-response'); error.winerror = 216
        self.process.side_effect = error
        with self.assertRaises(module.ZiniaoDriverError) as caught:
            self.ensure()
        self.assertIn('WinError 216', str(caught.exception))
        self.assertNotIn('private-', str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(self.leftovers(), [])

    def test_atomic_replace_failure_keeps_original_and_backup(self):
        self.target.write_bytes(b'old')
        with patch.object(module.os, 'replace', side_effect=PermissionError(errno.EACCES, 'private-error')):
            with self.assertRaises(module.ZiniaoDriverError):
                self.ensure()
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(next(self.folder.glob('*.bak')).read_bytes(), b'old')
        self.assertEqual(self.leftovers(), [])

    def test_target_changed_to_link_during_download_is_not_replaced(self):
        self.target.write_bytes(b'old')
        with patch.object(Path, 'is_symlink', side_effect=[False, False, True]):
            with self.assertRaisesRegex(module.ZiniaoDriverError, '下载期间变为链接'):
                self.ensure()
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(list(self.folder.glob('*.bak')), [])

    def test_directory_target_is_not_overwritten(self):
        self.target.mkdir()
        with self.assertRaises(module.ZiniaoDriverError):
            self.ensure()
        self.assertTrue(self.target.is_dir())
        self.session_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
