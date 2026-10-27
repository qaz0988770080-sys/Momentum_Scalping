"""Visible, persistent launcher diagnostics. Never accepts or logs credentials."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

def note(event, **values):
    row = dict(event=event, checked_at=datetime.now(timezone.utc).isoformat(), **values)
    root = Path(__file__).resolve().parent
    folder = root / "private"
    folder.mkdir(exist_ok=True)
    with (folder / "web-startup.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)

if __name__ == "__main__":
    try:
        note("starting", pid=os.getpid(), python=sys.version.split()[0])
        from taishin_readonly_web import main
        main()
    except KeyboardInterrupt:
        note("stopped")
    except BaseException as exc:
        note("fatal", error=type(exc).__name__, errno=getattr(exc, "errno", None),
             winerror=getattr(exc, "winerror", None))
        raise SystemExit(1)
