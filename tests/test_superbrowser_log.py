import io
import logging
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
_INSERTED_SUPERBROWSER_PATH = False
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))
    _INSERTED_SUPERBROWSER_PATH = True

from util.log import EncodingSafeStreamHandler

if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class _GbkOnlyStream(io.StringIO):
    @property
    def encoding(self):
        return "gbk"

    def write(self, value):
        value.encode(self.encoding)
        return super().write(value)


class EncodingSafeStreamHandlerTests(unittest.TestCase):
    def test_unencodable_currency_symbol_does_not_raise_logging_error(self):
        stream = _GbkOnlyStream()
        handler = EncodingSafeStreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "广告余额：฿1,925.30",
            (),
            None,
        )

        handler.emit(record)

        self.assertIn(r"\u0e3f1,925.30", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
