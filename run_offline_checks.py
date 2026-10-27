"""Run offline checks only. Keeps scratch directories for Windows sandbox compatibility."""
import importlib
import json
from pathlib import Path
import tempfile
from functools import partial
import unittest
import sys

def main():
    scratch = Path(__file__).resolve().parent / ".test-artifacts"
    scratch.mkdir(exist_ok=True)
    original = tempfile.TemporaryDirectory
    results = {}
    # Some Windows app sandboxes deny rmdir in their Temp directory.
    # Retain test-only files in this project's own scratch folder.
    tempfile.TemporaryDirectory = partial(original, dir=str(scratch), delete=False)
    try:
        for name in ["api_gateway_v2_7", "market_v1_6", "scanner_v2_8",
                     "intraday_v2_1", "trading_v3_9", "runtime_v4_1"]:
            try:
                value = importlib.import_module(name).run_self_test()
                results[name] = dict(ok=bool(value.get("ok")), checks=value.get("checks"),
                                     error=value.get("error"))
            except Exception as exc:
                results[name] = dict(ok=False, error=type(exc).__name__ + ": " + str(exc))
        suite = unittest.defaultTestLoader.loadTestsFromNames(["test_taishin_adapter", "test_taishin_readonly", "test_taishin_web", "test_taishin_journal", "test_taishin_futures", "test_taishin_futures_web"])
        run = unittest.TextTestRunner(verbosity=1).run(suite)
        results["taishin_adapter"] = dict(ok=run.wasSuccessful(), tests=run.testsRun)
    finally:
        tempfile.TemporaryDirectory = original
    ok = all(r["ok"] for r in results.values())
    print(json.dumps(dict(ok=ok, offline_only=True, results=results), ensure_ascii=False, indent=2))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
