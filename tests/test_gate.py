"""The gate's decision table, exercised against the shipped bash.

Every case here is one the old wall-clock gate got wrong, or one that a future
"simplification" of the gate would break.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest
from gate_harness import crons, run_gate

# 2026-10-27 is a Tuesday in CDT (DST ends 2026-11-01).
# 2027-01-12 is a Tuesday in CST.
SUMMER_TUE, SUMMER_WED = "2026-10-27", "2026-10-28"
WINTER_TUE, WINTER_WED = "2027-01-12", "2027-01-13"

RUNG1, RUNG2, RUNG5 = "0 15 * * 2", "0 16 * * 2", "0 1 * * 3"
CATCHUP = "0 14 * * 3"


def test_on_time():
    r = run_gate(local_date=SUMMER_TUE, local_hour=10, cron=RUNG1)
    assert r.proceed is True
    assert r.cycle == SUMMER_TUE


@pytest.mark.parametrize("hour", [12, 14, 17, 20])
def test_hours_late_still_sends(hour):
    """The bug that ate four runs: lateness must never read as 'wrong DST half'."""
    r = run_gate(local_date=SUMMER_TUE, local_hour=hour, cron=RUNG1)
    assert r.proceed is True, r.log


def test_early_rung_in_the_other_dst_half_defers():
    """Rung 1 is 09:00 CST in winter. It steps aside; it does not fail."""
    r = run_gate(local_date=WINTER_TUE, local_hour=9, cron=RUNG1)
    assert r.proceed is False
    assert "later rung will send" in r.reason
    assert r.rc == 0


def test_second_rung_covers_the_winter_hour():
    r = run_gate(local_date=WINTER_TUE, local_hour=10, cron=RUNG2)
    assert r.proceed is True


def test_no_later_rung_consults_the_hour():
    """A later rung that looked at the clock would reintroduce the silent week."""
    for cron in [c for c in crons() if c not in (RUNG1, CATCHUP)]:
        for hour in (9, 10, 15, 21):
            r = run_gate(local_date=SUMMER_TUE, local_hour=hour, cron=cron)
            assert r.proceed is True, f"{cron} at {hour}:00 -> {r.reason}"


def test_already_sent_today():
    r = run_gate(local_date=SUMMER_TUE, local_hour=13, cron=RUNG2, marker=SUMMER_TUE)
    assert r.proceed is False
    assert "already sent" in r.reason


def test_last_rung_of_the_day_sends():
    r = run_gate(local_date=SUMMER_TUE, local_hour=20, cron=RUNG5)
    assert r.proceed is True


def test_catchup_when_the_target_day_succeeded():
    r = run_gate(local_date=SUMMER_WED, local_hour=9, cron=CATCHUP, marker=SUMMER_TUE)
    assert r.proceed is False
    assert "already sent" in r.reason


def test_catchup_when_the_target_day_produced_nothing():
    r = run_gate(local_date=SUMMER_WED, local_hour=9, cron=CATCHUP, marker="2026-10-20")
    assert r.proceed is True
    assert r.cycle == SUMMER_TUE, "the catch-up must bill itself to Tuesday's cycle"


def test_catchup_is_idempotent_against_its_own_marker():
    """It writes Tuesday's date, so a repeat catch-up firing must skip."""
    first = run_gate(local_date=SUMMER_WED, local_hour=9, cron=CATCHUP, marker="2026-10-20")
    again = run_gate(local_date=SUMMER_WED, local_hour=11, cron=CATCHUP, marker=first.cycle)
    assert first.proceed is True and again.proceed is False


def test_catchup_in_winter():
    r = run_gate(local_date=WINTER_WED, local_hour=8, cron=CATCHUP, marker="2027-01-05")
    assert r.proceed is True
    assert r.cycle == WINTER_TUE


def test_catchup_refuses_a_stale_cycle():
    """Arriving on Friday, it must not mail Tuesday's three-day-old digest."""
    r = run_gate(local_date="2026-10-30", local_hour=9, cron=CATCHUP, marker="2026-10-20")
    assert r.proceed is False
    assert "too late" in r.reason


def test_stale_marker_from_last_week_does_not_suppress():
    r = run_gate(local_date=SUMMER_TUE, local_hour=10, cron=RUNG1, marker="2026-10-20")
    assert r.proceed is True


@pytest.mark.parametrize("marker", [
    f"{SUMMER_TUE}\n", f"  {SUMMER_TUE}  ", f"{SUMMER_TUE}\n\n", f"\t{SUMMER_TUE}\t\n",
])
def test_marker_whitespace_is_tolerated(marker):
    r = run_gate(local_date=SUMMER_TUE, local_hour=10, cron=RUNG1, marker=marker)
    assert r.proceed is False, f"{marker!r} should still compare equal"


@pytest.mark.parametrize("date,day", [
    ("2026-10-26", "Monday"), ("2026-10-28", "Wednesday"),
    ("2026-10-29", "Thursday"), ("2026-11-01", "Sunday"),
])
def test_wrong_weekday(date, day):
    r = run_gate(local_date=date, local_hour=10, cron=RUNG1)
    assert r.proceed is False, day
    assert "weekday" in r.reason


def test_manual_dispatch_is_never_gated():
    r = run_gate(local_date="2026-10-26", local_hour=3, cron="",
                 marker=SUMMER_TUE, event="workflow_dispatch")
    assert r.proceed is True
    assert "manual" in r.reason


def test_gate_always_exits_zero():
    """A skip is a decision, not a failure -- a red gate would mail an alarm."""
    for date in (SUMMER_TUE, SUMMER_WED, "2026-10-26"):
        for cron in crons():
            r = run_gate(local_date=date, local_hour=10, cron=cron, marker="2026-10-05")
            assert r.rc == 0, r.log
            assert r.proceed in (True, False), r.log
