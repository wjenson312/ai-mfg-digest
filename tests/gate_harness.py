"""Run the REAL gate script out of the workflow YAML, against a stubbed clock.

Nothing here reimplements the gate. It pulls `jobs.gate.steps[].run` straight
out of .github/workflows/weekly-digest.yml, substitutes the ${{ }} expressions,
puts a stub `date` early on PATH, and executes the shipped bash. If someone
edits the gate, these tests exercise the edit.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "weekly-digest.yml"

# `on:` is the YAML 1.1 boolean True once parsed. That is normal, not a bug.
ON_KEY = True


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def crons() -> list[str]:
    return [e["cron"] for e in workflow()[ON_KEY]["schedule"]]


def gate_script() -> str:
    """The gate step's shell, exactly as shipped."""
    steps = workflow()["jobs"]["gate"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"expected one run: step in the gate job, got {len(runs)}"
    return runs[0]


def _substitute(script: str, ctx: dict[str, str]) -> str:
    """Replace ${{ ... }} expressions using a context mapping."""
    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        if expr not in ctx:
            raise KeyError(f"no harness value for ${{{{ {expr} }}}}")
        return ctx[expr]
    return re.sub(r"\$\{\{(.*?)\}\}", repl, script)


_STUB = r'''#!/usr/bin/env python3
# Stub `date`. Answers +%F / +%u / +%H / '+%Y-%m-%d %H:%M %Z' and -d, from env.
import os, sys, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

argv = sys.argv[1:]
day = os.environ["FAKE_DATE"]
hour = int(os.environ["FAKE_HOUR"])
tz = ZoneInfo(os.environ.get("TZ") or "UTC")

# -d is checked BEFORE the format: the "yesterday" call passes both, and
# matching +%F first would hand back today.
if "-d" in argv:
    spec = argv[argv.index("-d") + 1]
    m = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2})\s+([+-]?\d+)\s+days?\s*", spec)
    if not m:
        sys.exit(f"stub date: cannot parse -d {spec!r}")
    base = datetime.strptime(m.group(1), "%Y-%m-%d")
    base += timedelta(days=int(m.group(2)))
    fmt = next((a for a in argv if a.startswith("+")), "+%F")
    print(base.strftime(fmt.lstrip("+").replace("%F", "%Y-%m-%d")))
    sys.exit(0)

now = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=tz)
fmt = next((a for a in argv if a.startswith("+")), "+%F")
fmt = fmt.lstrip("+")
if fmt == "%F":
    print(now.strftime("%Y-%m-%d"))
elif fmt == "%u":
    print(now.isoweekday())
elif fmt == "%H":
    print(f"{now.hour:02d}")
else:
    print(now.strftime(fmt.replace("%F", "%Y-%m-%d")))
'''


class Result:
    def __init__(self, proceed: bool | None, reason: str, cycle: str, log: str, rc: int):
        self.proceed, self.reason, self.cycle = proceed, reason, cycle
        self.log, self.rc = log, rc

    def __repr__(self) -> str:
        return f"Result(proceed={self.proceed}, cycle={self.cycle!r}, reason={self.reason!r})"


def run_gate(*, local_date: str, local_hour: int, cron: str,
             marker: str | None = None, event: str = "schedule") -> Result:
    """Execute the shipped gate script under a stubbed clock."""
    wf = workflow()
    env_block = {k: str(v) for k, v in wf["env"].items()}

    script = _substitute(gate_script(), {
        "github.event_name": event,
        "github.event.schedule": cron,
    })

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "bin").mkdir()
        stub = tmp / "bin" / "date"
        stub.write_text(_STUB)
        stub.chmod(0o755)

        marker_file = tmp / env_block["MARKER_FILE"]
        if marker is not None:
            marker_file.parent.mkdir(parents=True, exist_ok=True)
            marker_file.write_text(marker)

        out = tmp / "github_output"
        out.touch()

        env = {
            **os.environ,
            **env_block,
            "EVENT_NAME": event,
            "CRON": cron,
            "GITHUB_OUTPUT": str(out),
            "FAKE_DATE": local_date,
            "FAKE_HOUR": str(local_hour),
            "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
        }

        proc = subprocess.run(["bash", "-c", script], cwd=tmp, env=env,
                              capture_output=True, text=True)

        parsed: dict[str, str] = {}
        for line in out.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                parsed[k] = v

    proceed = {"true": True, "false": False}.get(parsed.get("proceed", ""))
    return Result(proceed, parsed.get("reason", ""), parsed.get("cycle_date", ""),
                  proc.stdout + proc.stderr, proc.returncode)


# --- helpers shared with the ladder tests -----------------------------------

def local_time_of_cron(cron: str, on_or_after: dt.date, tz: str) -> dt.datetime:
    """When does this UTC cron next fire, expressed in `tz` local time?"""
    minute, hour, _dom, _mon, dow = cron.split()
    target_dow = int(dow)          # cron: 0/7=Sun, 1=Mon .. 6=Sat
    d = on_or_after
    for _ in range(8):
        # cron dow -> isoweekday (Mon=1..Sun=7)
        iso = 7 if target_dow == 0 else target_dow
        if d.isoweekday() == iso:
            utc = dt.datetime(d.year, d.month, d.day, int(hour), int(minute),
                              tzinfo=dt.timezone.utc)
            return utc.astimezone(ZoneInfo(tz))
        d += dt.timedelta(days=1)
    raise AssertionError(f"no match for {cron}")
