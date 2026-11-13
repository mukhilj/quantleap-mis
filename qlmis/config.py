"""Configuration loading: settings.toml plus editable CSV side-files."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CAPITAL_COLUMNS = ["date", "client_id", "type", "amount_inr", "counterparty_client_id", "note"]
# Sign applied to amount_inr per flow type (amount always entered positive).
FLOW_SIGN = {"OPENING": 1, "DEPOSIT": 1, "WITHDRAWAL": -1, "TRANSFER_IN": 1, "TRANSFER_OUT": -1}
ALIAS_COLUMNS = ["client_id", "strategy_id", "source_name", "source_basket",
                 "display_name", "logical_strategy", "version", "notes"]


@dataclass
class Settings:
    root: Path
    raw: dict
    input_dirs: list[Path]
    database: Path
    archive_dir: Path
    export_dir: Path
    capital_file: Path
    aliases_file: Path
    holidays_file: Path
    exclude_dirs: list[str]
    platform: str
    timezone: str
    retention_days: int
    session_close_ist: str
    accounts: dict[str, str]
    calc: dict = field(default_factory=dict)
    charges: dict = field(default_factory=dict)


def _p(root: Path, v: str) -> Path:
    # Resolve a configured path relative to the project root.
    p = Path(v)
    return p if p.is_absolute() else (root / p)


def load_settings(root: Path | None = None) -> Settings:
    """Read config/settings.toml under `root` (defaults to the project folder)."""
    root = Path(root) if root else PROJECT_ROOT
    raw = tomllib.loads((root / "config" / "settings.toml").read_text(encoding="utf-8"))
    paths, src = raw.get("paths", {}), raw.get("source", {})
    return Settings(
        root=root,
        raw=raw,
        input_dirs=[_p(root, d) for d in paths.get("input_dirs", ["."])],
        database=_p(root, paths.get("database", "data/ledger.sqlite")),
        archive_dir=_p(root, paths.get("archive_dir", "archive")),
        export_dir=_p(root, paths.get("export_dir", "exports")),
        capital_file=_p(root, paths.get("capital_file", "config/capital.csv")),
        aliases_file=_p(root, paths.get("aliases_file", "config/strategy_aliases.csv")),
        holidays_file=_p(root, paths.get("holidays_file", "config/holidays.csv")),
        exclude_dirs=paths.get("exclude_dirs", []),
        platform=src.get("platform", "quantlead"),
        timezone=src.get("timezone", "Asia/Kolkata"),
        retention_days=int(src.get("retention_days", 90)),
        session_close_ist=src.get("session_close_ist", "15:30"),
        accounts=dict(raw.get("accounts", {})),
        calc={
            "breakeven_tolerance_inr": 0.0, "drawdown_tolerance_inr": 1.0,
            "annualisation_days": 252, "risk_free_annual": 0.0,
            "sortino_target_annual": 0.0, "min_days_for_annualised": 252,
            **raw.get("calc", {}),
        },
        charges=dict(raw.get("charges", {})),
    )


def load_capital(s: Settings) -> pd.DataFrame:
    """Dated capital flows; signed_amount applies the FLOW_SIGN convention."""
    if not s.capital_file.exists():
        return pd.DataFrame(columns=CAPITAL_COLUMNS + ["signed_amount"])
    df = pd.read_csv(s.capital_file, dtype=str, keep_default_na=False)
    for c in CAPITAL_COLUMNS:
        if c not in df:
            df[c] = ""
    df = df[CAPITAL_COLUMNS].copy()
    df = df[df["date"].str.strip() != ""]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["type"] = df["type"].str.upper().str.strip()
    bad = ~df["type"].isin(FLOW_SIGN)
    if bad.any():
        raise ValueError(f"Unknown capital flow type(s): {sorted(df.loc[bad, 'type'].unique())}")
    df["amount_inr"] = pd.to_numeric(df["amount_inr"])
    df["signed_amount"] = df["amount_inr"].abs() * df["type"].map(FLOW_SIGN)
    return df.sort_values(["date", "client_id"]).reset_index(drop=True)


def save_capital(s: Settings, df: pd.DataFrame) -> None:
    """Write the capital table back (validated by re-loading)."""
    out = df.reindex(columns=CAPITAL_COLUMNS).copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    tmp = s.capital_file.with_suffix(".tmp")
    out.to_csv(tmp, index=False)
    tmp.replace(s.capital_file)
    load_capital(s)  # raises if the saved content is invalid


def load_holidays(s: Settings) -> set:
    if not s.holidays_file.exists():
        return set()
    df = pd.read_csv(s.holidays_file, dtype=str, keep_default_na=False)
    return set(pd.to_datetime(df["date"]).dt.date)


def load_aliases(s: Settings) -> pd.DataFrame:
    if not s.aliases_file.exists():
        return pd.DataFrame(columns=ALIAS_COLUMNS)
    df = pd.read_csv(s.aliases_file, dtype=str, keep_default_na=False)
    return df.reindex(columns=ALIAS_COLUMNS, fill_value="")


def sync_aliases(s: Settings, strategies: pd.DataFrame) -> int:
    """Append newly seen strategies to the alias file; never overwrite user edits.

    `strategies` needs client_id, strategy_id, strategy_name, basket_name.
    Returns number of rows added.
    """
    cur = load_aliases(s)
    known = set(zip(cur["client_id"], cur["strategy_id"]))
    new = strategies[[ (c, i) not in known for c, i in zip(strategies.client_id, strategies.strategy_id)]]
    if new.empty:
        return 0
    add = pd.DataFrame({
        "client_id": new.client_id, "strategy_id": new.strategy_id,
        "source_name": new.strategy_name, "source_basket": new.basket_name,
        "display_name": new.strategy_name, "logical_strategy": "", "version": "", "notes": "",
    })
    out = pd.concat([cur, add], ignore_index=True).sort_values(["client_id", "source_basket", "source_name"])
    s.aliases_file.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(s.aliases_file, index=False)
    return len(add)
