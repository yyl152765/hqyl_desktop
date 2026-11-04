"""Run the standalone read-only diagnostic against local synthetic binaries."""
from pathlib import Path
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/diagnose_ziniao_driver.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell diagnostic')
class DriverDiagnosticScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='private-path-canary-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def run_check(self, major=123, directory=None):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                 str(SCRIPT), '-DriverDirectory', str(directory or self.folder),
                                 '-CoreMajor', str(major)], capture_output=True, timeout=15,
                                encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.stderr, '')
        self.assertNotIn('private-path-canary', result.stdout)
        data = json.loads(result.stdout.lstrip('\ufeff'))
        self.assertEqual(result.returncode, 0 if data['status'] == 'passed' else 1)
        return data

    def compile_fixture(self, body, major=123):
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            self.skipTest('Local .NET C# compiler unavailable for harmless fixture')
        source = self.folder / 'fixture.cs'
        source.write_text('class Fixture { static void Main(string[] args) { ' + body + ' } }', encoding='utf-8')
        target = self.folder / f'chromedriver{major}.exe'
        result = subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(target), str(source)],
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace'))
        return target

    def test_missing_directory_and_missing_file_are_safe(self):
        missing = self.folder / 'absent-private-path'
        self.assertEqual(self.run_check(directory=missing)['code'], 'driver_directory_missing')
        self.assertFalse(missing.exists())
        self.assertEqual(self.run_check()['code'], 'driver_file_missing')
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_zero_bytes_and_html_are_not_executed_or_modified(self):
        target = self.folder / 'chromedriver123.exe'
        for value in (b'', b'<html>private-download-error</html>'):
            with self.subTest(value=value):
                target.write_bytes(value)
                report = self.run_check()
                self.assertEqual(report['code'], 'invalid_pe_signature_not_executed')
                self.assertFalse(report['process_started'])
                self.assertIsNone(report['winerror'])  # No invented Windows error.
                self.assertEqual(target.read_bytes(), value)

    def test_bad_pe_start_retains_numeric_windows_error(self):
        target = self.folder / 'chromedriver123.exe'
        data = bytearray(128)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 60, 64)
        data[64:68] = b'PE\x00\x00'
        struct.pack_into('<H', data, 68, 0x14c)
        target.write_bytes(data)
        report = self.run_check()
        self.assertEqual(report['code'], 'driver_start_failed')
        self.assertIsInstance(report['winerror'], int)
        self.assertGreater(report['winerror'], 0)
        self.assertEqual(target.read_bytes(), data)

    def test_harmless_fixture_version_and_mismatch(self):
        target = self.compile_fixture('System.Console.WriteLine("ChromeDriver 123.0.1.2 (fixture)");')
        before = target.read_bytes()
        report = self.run_check()
        self.assertEqual(report['driver_version'], '123.0.1.2')
        self.assertTrue(report['major_matches'])
        self.assertEqual(report['status'], 'passed')
        shutil.copyfile(target, self.folder / 'chromedriver124.exe')
        self.assertEqual(self.run_check(major=124)['code'], 'driver_major_mismatch')
        self.assertEqual(target.read_bytes(), before)

    def test_unrelated_executable_output_is_not_accepted_or_disclosed(self):
        self.compile_fixture('System.Console.WriteLine("private-output-canary");')
        report = self.run_check()
        self.assertEqual(report['code'], 'driver_version_unrecognized')
        self.assertIsNone(report['driver_version'])
        self.assertNotIn('private-output-canary', json.dumps(report))

    def test_timeout_stops_only_launched_fixture(self):
        self.compile_fixture('System.Threading.Thread.Sleep(60000);')
        report = self.run_check()
        self.assertEqual(report['code'], 'driver_version_timeout')
        self.assertTrue(report['terminated_own_process'])

    def test_script_has_utf8_bom_for_windows_powershell_5(self):
        self.assertTrue(SCRIPT.read_bytes().startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main()
