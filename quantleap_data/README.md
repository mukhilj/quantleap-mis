# quantleap_data

Put your QuantLead **positions** CSV exports here. Subfolders are scanned recursively, so organise however you like. Suggested: one folder per export date.

```
quantleap_data/
  2026-11-13/
    AB1234 positions_....csv
    CD5678 positions_....csv
  2026-12-15/
    ...
```

- **Overlapping exports are safe.** Rows already in the ledger are counted as duplicates, never added twice.
- **Never delete old exports.** QuantLead keeps only about 90 days of detail, and these files are your raw evidence. Imports also keep a copy of each file in `archive/`.
- **Nothing here is ever committed to git.** `*.csv` is git-ignored.
