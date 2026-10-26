"""Invariants of the cron ladder itself, pinned in both halves of the DST year.

These are the tests that stop someone "tidying up" a cron line and silently
moving a rung onto the wrong local weekday -- the class of mistake that made
the original schedule unreliable.
"""
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import pytest
from gate_harness import ON_KEY, crons, local_time_of_cron, run_gate, workflow

WF = workflow()
TZ = WF["env"]["TARGET_TZ"]
TARGET_DOW = int(WF["env"]["TARGET_DOW"])          # 2 = Tuesday
TARGET_HOUR = int(WF["env"]["TARGET_HOUR"])        # 10
FIRST_RUNG = WF["env"]["FIRST_RUNG"]
CATCHUP = WF["env"]["CATCHUP_RUNG"]

# One Tuesday in each half of the DST year. US DST in 2026/27: 2026-03-08 to
# 2026-11-01, then 2027-03-14.
SUMMER_TUE = dt.date(2026, 10, 27)   # CDT, UTC-5
WINTER_TUE = dt.date(2027, 1, 12)    # CST, UTC-6
BOTH_HALVES = [pytest.param(SUMMER_TUE, id="CDT"), pytest.param(WINTER_TUE, id="CST")]

MAIN_RUNGS = [c for c in crons() if c != CATCHUP]


def test_the_two_halves_really_are_different_offsets():
    """Guard the fixtures themselves: a wrong date would make every test vacuous."""
    off = {d: dt.datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(TZ)).utcoffset()
           for d in (SUMMER_TUE, WINTER_TUE)}
    assert off[SUMMER_TUE] == dt.timedelta(hours=-5)
    assert off[WINTER_TUE] == dt.timedelta(hours=-6)


def test_ladder_has_several_rungs():
    """One cron a week is one chance a week."""
    assert len(MAIN_RUNGS) >= 4, MAIN_RUNGS
    assert CATCHUP in crons()


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_every_main_rung_lands_on_the_target_weekday(tuesday):
    for cron in MAIN_RUNGS:
        local = local_time_of_cron(cron, tuesday - dt.timedelta(days=3), TZ)
        assert local.isoweekday() == TARGET_DOW, (
            f"{cron} fires {local:%A %Y-%m-%d %H:%M %Z} -- not the target weekday")


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_catchup_lands_the_morning_after(tuesday):
    local = local_time_of_cron(CATCHUP, tuesday, TZ)
    assert local.isoweekday() == TARGET_DOW + 1
    assert local.date() == tuesday + dt.timedelta(days=1)
    assert local.hour < 12, "a catch-up in the afternoon is not a catch-up"


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_rungs_are_spread_across_the_day(tuesday):
    """Rungs bunched into one hour share one outage. Spread them out."""
    hours = sorted(local_time_of_cron(c, tuesday - dt.timedelta(days=3), TZ).hour
                   for c in MAIN_RUNGS)
    assert hours[-1] - hours[0] >= 8, f"ladder spans only {hours}"
    assert len(set(hours)) == len(hours), f"duplicate rung hours: {hours}"


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_first_rung_is_the_earliest_rung(tuesday):
    """The soft hour preference is only safe if FIRST_RUNG really is first."""
    times = {c: local_time_of_cron(c, tuesday - dt.timedelta(days=3), TZ)
             for c in MAIN_RUNGS}
    assert min(times, key=times.get) == FIRST_RUNG


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_some_rung_hits_the_target_hour(tuesday):
    hours = [local_time_of_cron(c, tuesday - dt.timedelta(days=3), TZ).hour
             for c in MAIN_RUNGS]
    assert TARGET_HOUR in hours, f"no rung at {TARGET_HOUR}:00 local; got {hours}"


# --- whole-week simulations --------------------------------------------------

def _run_week(tuesday, *, lateness=0, fail_rungs=(), marker="2026-01-01"):
    """Walk the ladder in firing order, threading the marker as the workflow does.

    `fail_rungs` are rungs whose firing GitHub dropped, or whose pipeline died
    before the marker was written. Returns the list of rungs that sent.
    """
    sent = []
    schedule = sorted(
        ((local_time_of_cron(c, tuesday - dt.timedelta(days=3), TZ), c)
         for c in crons()),
        key=lambda p: p[0],
    )
    for local, cron in schedule:
        if cron in fail_rungs:
            continue
        hour = min(local.hour + lateness, 23)
        r = run_gate(local_date=local.date().isoformat(), local_hour=hour,
                     cron=cron, marker=marker)
        assert r.rc == 0, r.log
        if r.proceed:
            sent.append((cron, hour))
            marker = r.cycle          # the "Record the send" step
    return sent, marker


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_happy_path_sends_exactly_once_at_the_target_hour(tuesday):
    sent, marker = _run_week(tuesday)
    assert len(sent) == 1, f"expected one email, got {sent}"
    cron, hour = sent[0]
    assert hour == TARGET_HOUR, f"sent at {hour}:00, wanted {TARGET_HOUR}:00"
    assert marker == tuesday.isoformat()


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
@pytest.mark.parametrize("lateness", [1, 2, 4, 7])
def test_lateness_never_causes_a_skip(tuesday, lateness):
    """Deliveries measured seven hours late must still produce exactly one email."""
    sent, marker = _run_week(tuesday, lateness=lateness)
    assert len(sent) == 1, f"{lateness}h late -> {sent}"
    assert marker == tuesday.isoformat()


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_any_single_dropped_rung_still_delivers(tuesday):
    for drop in crons():
        sent, marker = _run_week(tuesday, fail_rungs=(drop,))
        assert len(sent) == 1, f"dropping {drop} gave {sent}"
        assert marker == tuesday.isoformat()


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_all_but_the_last_rung_dropped_still_delivers(tuesday):
    """The whole point of a ladder: the final rung alone carries the week."""
    sent, marker = _run_week(tuesday, fail_rungs=tuple(MAIN_RUNGS[:-1]))
    assert len(sent) == 1, sent
    assert sent[0][0] == MAIN_RUNGS[-1]
    assert marker == tuesday.isoformat()


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_catchup_fires_only_when_the_target_day_produced_nothing(tuesday):
    # Every main rung dropped -> the catch-up is the one that sends.
    sent, marker = _run_week(tuesday, fail_rungs=tuple(MAIN_RUNGS))
    assert [c for c, _ in sent] == [CATCHUP], sent
    assert marker == tuesday.isoformat(), "catch-up must record Tuesday's cycle"

    # Target day succeeded -> the catch-up must stay quiet.
    sent, _ = _run_week(tuesday)
    assert CATCHUP not in [c for c, _ in sent]


@pytest.mark.parametrize("tuesday", BOTH_HALVES)
def test_two_rungs_arriving_together_do_not_both_send(tuesday):
    """What workflow-level concurrency buys: serialised, the second one skips."""
    first = run_gate(local_date=tuesday.isoformat(), local_hour=14,
                     cron=MAIN_RUNGS[1], marker="2026-01-01")
    second = run_gate(local_date=tuesday.isoformat(), local_hour=14,
                      cron=MAIN_RUNGS[2], marker=first.cycle)
    assert first.proceed is True and second.proceed is False


def test_concurrency_is_configured_and_does_not_cancel():
    c = WF["concurrency"]
    assert c["cancel-in-progress"] is False
    assert c["group"]


def test_alarm_is_gated_to_scheduled_failures():
    cond = WF["jobs"]["alarm"]["if"]
    assert "always()" in cond
    assert "github.event_name == 'schedule'" in cond
    assert "failure" in cond


def test_marker_step_runs_even_when_a_later_step_fails():
    step = next(s for s in WF["jobs"]["digest"]["steps"]
                if s.get("name") == "Record the send")
    assert "always()" in step["if"]
    assert "outputs.mode == 'send'" in step["if"], "never on dry-run or preview"
    assert "EMAIL_RECIPIENTS" in step["run"], "must verify the recipient list"


def test_pipeline_runs_in_the_target_timezone():
    """Runners are UTC; a late rung would otherwise file tomorrow's filename."""
    step = next(s for s in WF["jobs"]["digest"]["steps"] if s.get("id") == "run")
    assert step["env"]["TZ"] == "${{ env.TARGET_TZ }}"


def test_alarm_ladder_matches_the_real_crons():
    """The alarm hard-codes the ladder so it needs no checkout. Keep it in sync."""
    import ast
    run = WF["jobs"]["alarm"]["steps"][0]["run"].splitlines()
    body = "\n".join(run[1:run.index("PY")])
    tree = ast.parse(body)

    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name == "ladder":
                found["ladder"] = ast.literal_eval(node.value)
            elif name == "local":
                found["local"] = ast.literal_eval(node.value)

    assert found["ladder"] == crons(), (
        "the alarm's ladder list has drifted from the cron schedule")
    assert set(found["local"]) == set(crons()), (
        "every cron needs a local-time label in the alarm")


def test_alarm_heredoc_terminates_and_compiles():
    """YAML strips block indentation; the heredoc delimiter must land at col 0."""
    import ast
    run = WF["jobs"]["alarm"]["steps"][0]["run"].splitlines()
    assert run[0].strip() == "python3 - <<'PY'"
    assert "PY" in run, "closing delimiter not at column 0 -- heredoc never ends"
    ast.parse("\n".join(run[1:run.index("PY")]))


def test_alarm_needs_no_checkout_or_pip():
    """It has to work on the day everything else didn't."""
    job = WF["jobs"]["alarm"]
    text = str(job)
    assert "actions/checkout" not in text
    assert "pip install" not in text
    assert "setup-python" not in text


@pytest.mark.parametrize("job", ["gate", "digest"])
def test_checkout_takes_the_branch_tip_not_the_run_sha(job):
    """A scheduled run's default checkout is the sha the run was CREATED from.

    A rung that waited in the concurrency queue while an earlier rung pushed the
    marker would then read the pre-marker tree and send a duplicate -- and in
    the digest job a detached HEAD cannot push the marker at all.
    """
    step = next(s for s in WF["jobs"][job]["steps"]
                if str(s.get("uses", "")).startswith("actions/checkout"))
    assert step.get("with", {}).get("ref") == "${{ github.ref_name }}", (
        f"{job} must check out the branch tip, not the pinned run sha")
