#### Data basis
- **Source:** QuantLead *positions* export. **One row = one option-leg position**, from entry fill to full exit.
- **Confirmed performance** includes only filled legs (`entry_status = COMPLETE`). Cancelled and rejected rows stay in the ledger but are excluded.
- **P&L** is the source `pnl`: realised, **gross** (no charges) and per leg. It is checked against (exit − entry) × qty × side. Each leg is dated by its **close date**.
- **Derived run** = all legs a strategy armed on one `execution_date`. QuantLead does not provide this grouping; it is derived here. A run's P&L counts only the legs that closed inside the selected range.
- **Estimated net** = gross minus modelled charges (`settings.toml [charges]`). It is hypothetical and never replaces gross.

#### Coverage
- A day is **covered** when an imported export proves the account's detail is complete for that day. A covered day with no closes is a **verified ₹0**.
- **Partial** means the export was taken mid-session. **Missing** means unknown: it is never treated as zero, never drawn as a flat line, and never annualised over.
- A selection's day is missing if **any** included account is missing that day.

#### Trade statistics (unit = leg or run)
- Win: P&L > tolerance. Loss: P&L < −tolerance. Otherwise breakeven. Tolerance = `breakeven_tolerance_inr`.
- **Win rate** = wins ÷ all outcomes (breakevens stay in the denominator). The tooltip also shows wins ÷ (wins + losses).
- **Payoff ratio** = avg win ÷ |avg loss|. **Profit factor** = gross wins ÷ |gross losses|. With no losses it is ∞; with no outcomes it is n/a. It is never 0 by default.
- **Expectancy** = P(win) × avg win − P(loss) × |avg loss|. This equals the mean P&L per unit.
- **Streaks** are counted in close-date order, then entry time; a breakeven resets the streak. Same-day order is approximate because there is no exit timestamp. Day streaks count only days that had closes.

#### Drawdowns (₹)
- Path = cumulative realised P&L at daily close over observed days. It starts from a **baseline** (₹0 before the first observation), so a first-day loss counts as a drawdown.
- **Underwater**: below the running peak by more than `drawdown_tolerance_inr`. **Recovered**: back within the tolerance of the peak. **New high**: strictly above the peak.
- Each episode reports: peak date, trough date, recovery date (blank if ongoing), depth, peak→trough, trough→recovery and total underwater time. Durations are given in calendar days and in sessions (observations).
- **Deepest**, **longest underwater**, **recovery time of the deepest** and **current** drawdown are reported separately, because they can come from different episodes.
- **Time without a new high** includes flat periods at the peak.
- **Baseline mode.** *Reset* measures from ₹0 at the range start. *Carry* continues earlier observed history, so an episode that began before the range keeps its original peak.
- Drawdowns cover **observed history only**. If an episode's peak is "start of observed history", the true start may be earlier and is unknown.
- Intraday and intratrade drawdowns cannot be measured from closed positions.

#### Capital-based returns (whole accounts only)
- **Equity** = dated flows (`config/capital.csv`) + cumulative realised P&L. Flows apply at the start of their date.
- **Daily return** = day P&L ÷ (prior equity + same-day flows). Chaining these gives the time-weighted (TWR) index, so deposits never count as profit. Transfers between two included accounts net out.
- **CAGR** = TWR^(365.25 / calendar days observed) − 1. It never uses a requested start date that falls before the data.
- **Volatility / Sharpe / Sortino** use daily returns, scaled by √`annualisation_days`. Sharpe uses `risk_free_annual`. Sortino uses downside deviation below `sortino_target_annual`.
- **Max DD %** is measured on the TWR index. **Calmar-style** = CAGR ÷ |max DD %| over the same window. **Recovery factor** = P&L ÷ |max ₹ DD|.
- **XIRR** is the money-weighted (investor) return from flows and ending equity.
- % metrics are **unavailable for strategy subsets**, because capital is recorded per account.
- Below `min_days_for_annualised` days, annualised figures carry a short-history warning.

#### Portfolio
- **Correlation** = Pearson correlation of daily realised P&L over observed days (no-trade days = ₹0), or optionally over days when both traded. A minimum observation count applies and n is shown.
- **Contributions** are rupee sums. **Leave-one-out** and **"excluding best N"** are hypothetical sensitivities on the same observed days.
- **Basket selection** = union of the chosen baskets (each system counted once), then narrowed by account, system and instrument filters.

#### Holding duration
- Holding duration = sessions (trading days) from the entry fill date to the close date. The source has no exit time. This is separate from drawdown duration.
