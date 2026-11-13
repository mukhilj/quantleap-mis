# QuantLeap Performance MIS

This is a local dashboard for reviewing the history of QuantLead automated systems across one or more broker accounts. It only reads the CSV exports you download from QuantLead: it does not connect to brokers, never places trades, and never sends your data anywhere. Everything stays on your PC.

## 0. Quick start (new user)

1. Download this repo (green **Code** button → *Download ZIP*, or `git clone`) and unzip it anywhere.
2. Do the one-time setup in section 1.
3. In QuantLead, export the **positions** CSV for each account (pick the widest date range it allows) and drop the files into `inbox\`.
4. Copy `config\capital.example.csv` to `config\capital.csv` and enter your own capital per account (optional: without it you get ₹ metrics only, no % returns).
5. Double-click `import_exports.bat`, type Y, then double-click `run_dashboard.bat`.

Accounts are picked up automatically from the files: one account or many, no configuration needed. Re-export every few weeks (QuantLead keeps only ~90 days of detail) and import again; the local ledger keeps the full history and never double-counts.

**Your data is never committed.** `.gitignore` excludes all CSVs, the ledger, the archive and exports, so if you fork this repo your trades stay private.

## 1. One-time setup

1. Install Python 3.13 or 3.14 from python.org (tick *Add python.exe to PATH* during install).
2. Open PowerShell in this folder and run:
   ```
   python -m pip install --user -r requirements.txt
   ```
   > If Windows Smart App Control is on, it may block DLLs inside a new virtual environment ("An Application Control policy has blocked this file"). The `--user` install shown above avoids that.
3. Optional check: `python -m pytest -q tests`. All tests should pass.

## 2. Launch

Double-click **`run_dashboard.bat`**. The dashboard opens at http://localhost:8501. Close the black window to stop it.

Alternatively, run `python -m streamlit run app.py`.

## 3. Importing new exports

1. Download the QuantLead positions CSVs and drop them into this folder or into `inbox\`. The filenames don't matter.
2. Then do either of the following:
   - Click **Import / refresh** in the dashboard sidebar.
   - Double-click **`import_exports.bat`**. It shows a preview first, then asks Y/N before applying.
   - Run `python import_now.py --preview`, then `python import_now.py`.

What happens during an import:

- **Files** are identified by a SHA-256 hash. Re-importing the same file, or a renamed copy of it, is skipped.
- **Records** are identified by `client_id + position_id`. If an overlapping export contains rows already imported, those rows are counted as duplicates, so nothing is double-counted.
- **Each import is classified** into these outcomes:
  - **new**: a record not seen before.
  - **duplicate**: identical to an existing record.
  - **update**: an open position that has since closed, or blank fields that are now filled in. The previous version is kept as a revision.
  - **superseded**: an older snapshot that arrived after a newer one.
  - **conflict**: the same record with different values and no rule saying which version is correct. It goes to the review queue.
  - **invalid**: a row that fails validation. It is quarantined.
- **Conflicts and ambiguous rows** are excluded from results until you accept or reject them in the **Data & Methodology** tab.
- **The whole import is one transaction**, so if it fails nothing changes. Each source file is copied to `archive\<hash>__<name>`.
- **Old trades are never deleted** when QuantLead stops including them in its exports (it purges after about 90 days). The local ledger keeps them permanently.
- **Older exports can be backfilled**: drop them in and import them the same way.
- **Unrecognised CSV layouts** are flagged and not imported.

### Source-reported aggregates (optional)

To keep QuantLead KPI rollups, save them as a CSV with these columns:

`snapshot_date,client_id,scope,period_start,period_end,metric,value,basis,is_cumulative`

They are stored and shown separately. They are never added to the trade-level data.

## 4. Configuration (all editable files)

| File | What it controls |
|---|---|
| `config/settings.toml` | Paths, account labels, retention, breakeven/drawdown tolerances, annualisation, risk-free rate, Sortino target, charge-estimate rates |
| `config/capital.csv` | Dated capital events: OPENING, DEPOSIT, WITHDRAWAL, TRANSFER_IN, TRANSFER_OUT. Amounts are entered positive. It can also be edited in the Data tab. |
| `config/strategy_aliases.csv` | Display names, versions and `logical_strategy` (use it to link the same logic across accounts). New strategies are added to it automatically. |
| `config/holidays.csv` | Exchange holidays. These days are never counted as missing or as zero. **Verify against the NSE circular.** |
| Baskets | Created and edited in the **Systems & Baskets** tab and stored in the database. `QL · …` baskets are created automatically from QuantLead's basket names. |

## 5. Backup and restore

Back up these folders: **`data\`** (the ledger), **`config\`** and **`archive\`** (the original exports).

```
# backup (PowerShell) — stop the dashboard first
Compress-Archive -Path data,config,archive -DestinationPath "..\qlmis_backup_$(Get-Date -f yyyyMMdd).zip"
```

To restore: stop the dashboard, unzip the backup into this folder (overwriting `data\`, `config\` and `archive\`), then relaunch.

If only `archive\` survives, delete `data\ledger.sqlite`, copy the archived CSVs into `inbox\` and import them. The ledger is rebuilt exactly. The only things lost are basket edits and resolved reviews, so keep backups of `data\` as well.

If the folder is synced by OneDrive/Dropbox/Google Drive, don't open the dashboard on two PCs at once, because SQLite and sync conflicts don't mix.

## 6. Data dictionary (ledger table `legs`)

| Field | Meaning |
|---|---|
| `leg_key` | `quantlead\|client_id\|position_id`, the permanent identity of a record |
| `client_id` | Account |
| `strategy_id` / `strategy_name` | System identity / name. Names are **not unique**: two different strategies can share a name, so `strategy_id` is the identity. |
| `basket_name` | QuantLead's own basket |
| `execution_date` | Date the strategy armed the trade. The fill may happen the next session. |
| `entry_filled_at` | Entry fill timestamp (IST) |
| `closed_date` | Date the position fully closed, and the date P&L is booked. There is no exit timestamp in the source. |
| `action` / `quantity` | BUY or SELL / units (the `lots` column is always 0 in the source) |
| `expected_entry_price` | Strategy reference price, used to calculate entry slippage |
| `average_entry_price` / `exit_average_price` | Average fill prices |
| `pnl` | Realised, gross, total for the leg |
| `exit_type` | EXIT, SL, EOD, EXPIRY_SETTLED or STRATEGY_SL |
| `entry_status` | COMPLETE, CANCELLED or REJECTED. Only COMPLETE is counted. |
| `revision`, `first_file_id/row`, `last_file_id/row` | Audit trail and provenance |

Other tables:

- `leg_revisions`: every accepted version of a record.
- `sightings`: every file and line where a record appeared.
- `review_queue`: conflicts and invalid rows.
- `coverage`: proof of which days are covered.
- `source_files` and `import_runs`: the import log.
- `baskets` and `basket_members`: named baskets.
- `rollups`: source-reported aggregates.

Calculation definitions are in `docs/METHODOLOGY.md`, which is also shown in the Data tab.
