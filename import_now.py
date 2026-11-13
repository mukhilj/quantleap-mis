"""Command-line import: `python import_now.py` (add --preview for a dry run)."""
import json
import sys
from pathlib import Path

from qlmis.config import load_settings
from qlmis.ingest import run_import
from qlmis.fmt import inr


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console: allow ₹
    preview = "--preview" in sys.argv
    s = load_settings()
    rep = run_import(s, dry_run=preview)
    print(f"Import {'PREVIEW (nothing saved)' if preview else 'run'} #{rep.run_id}: {rep.status}")
    for f in rep.files:
        print(f"  [{f.status:12}] {Path(f.path).name}  rows={f.rows} closes={f.date_min}..{f.date_max} {f.counts}")
        for w in f.warnings[:5]:
            print(f"      ! {w}")
    print("  totals:", json.dumps(rep.counts))
    print(f"  confirmed legs {rep.legs_before} -> {rep.legs_after};  gross P&L {inr(rep.pnl_before)} -> {inr(rep.pnl_after)}")
    if rep.counts["conflict"]:
        print(f"  {rep.counts['conflict']} conflict(s) need review (potential P&L impact {inr(rep.conflict_pnl_impact)}) - see Data tab")
    if rep.message:
        print("  ERROR:", rep.message)
    return 0 if rep.status in ("committed", "preview") else 1


if __name__ == "__main__":
    sys.exit(main())
