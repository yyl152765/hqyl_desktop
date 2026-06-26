"""PyInstaller entry point kept outside launcher/ to avoid shadowing the business main package."""

from __future__ import annotations

import sys

from launcher.main import main, self_check


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    main()
