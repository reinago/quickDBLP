"""A stand-in for ponder_dblp in the refresh tests: writes the fixture snapshot into the current directory.

STUB_MODE: ok | grow (one author more) | crash (exit code 3) | failed_records | dangling (the last paper/author row is
missing, like the old exporter did).   STUB_DELAY: seconds to pretend to work.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import write_snapshot  # noqa: E402

dump = Path(sys.argv[1])
mode = os.environ.get("STUB_MODE", "ok")
delay = float(os.environ.get("STUB_DELAY", 0))

print("[=>        ]  10 %\r", end="", flush=True)
time.sleep(delay / 2)
print("[=====>    ]  60 %\r", end="", flush=True)
time.sleep(delay / 2)
if mode == "crash":
    print("ERROR: boom", file=sys.stderr)
    sys.exit(3)

here = Path.cwd()
write_snapshot(here, extra_author=(mode == "grow"))
meta_path = here / "dblp_metadata.json"
meta = json.loads(meta_path.read_text())
meta["source_file_last_write_time"] = datetime.fromtimestamp(dump.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
meta["records_failed"] = 2 if mode == "failed_records" else 0
if mode == "dangling":
    for name in ("dblp_papers.csv", "dblp_authors.csv"):
        f = here / name
        f.write_text("".join(l for l in f.read_text(encoding="utf-8").splitlines(True) if not l.startswith("6\t")), encoding="utf-8")
    meta["total_papers"] -= 1
    meta["total_authors"] -= 1
meta_path.write_text(json.dumps(meta))
print("100 %")
