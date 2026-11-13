"""Headless render of the dashboard across filter combinations (Streamlit AppTest)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
DB = Path(__file__).resolve().parent.parent / "data" / "ledger.sqlite"
pytestmark = pytest.mark.skipif(not DB.exists(), reason="ledger not imported yet")


def _sample():
    """Pick real filter values from whatever ledger exists (works for any user's data)."""
    if not DB.exists():
        return None, [], None, None
    from qlmis.config import load_holidays, load_settings
    from qlmis.data import load_baskets, load_legs
    from qlmis.db import connect
    s = load_settings()
    con = connect(s.database)
    legs = load_legs(con, s, load_holidays(s))
    baskets = sorted(load_baskets(con))[:2]
    con.close()
    f = legs[legs.filled]
    return sorted(f.client_id.unique())[0], baskets, sorted(f.instrument.dropna().unique())[0], f.close_date.min()


ACCT, BASKETS, INSTR, FIRST_CLOSE = _sample()


def run(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def test_default_render():
    at = run()
    assert len(at.tabs) == 6
    assert any("Total P&L" in m.label for m in at.metric)


@pytest.mark.parametrize("state", [
    dict(f_accounts=[ACCT]),
    dict(f_baskets=BASKETS),
    dict(f_unit="Derived runs", f_cost="Estimated net (hypothetical)"),
    dict(f_ddmode="Carry prior peak"),
    dict(f_instr=[INSTR]),
])
def test_filtered_renders(state):
    run(**state)


def test_requested_period_before_coverage_warns():
    from datetime import timedelta
    at = run(f_range=(FIRST_CLOSE - timedelta(days=60), FIRST_CLOSE + timedelta(days=10)))
    assert any("missing — not zero" in w.value for w in at.warning)


def test_empty_range():
    from datetime import timedelta
    run(f_range=(FIRST_CLOSE - timedelta(days=200), FIRST_CLOSE - timedelta(days=170)))
