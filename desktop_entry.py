"""PyInstaller entry point kept outside launcher/ to avoid shadowing the business main package."""

from __future__ import annotations

import sys

from launcher.main import main, self_check


if __name__ == "__main__":
    if "--check-ads-database" in sys.argv:
        from backend.ads_database import prepare_runtime_database
        try:
            prepare_runtime_database()
        except Exception as error:
            print("ADS_DATABASE_CHECK=failed", type(error).__name__, flush=True)
            raise SystemExit(1) from None
        print("ADS_DATABASE_CHECK=passed", flush=True)
        raise SystemExit(0)
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    main()
