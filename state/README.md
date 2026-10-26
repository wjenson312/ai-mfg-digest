# state/

`last_sent` holds the **local date (America/Chicago) of the last genuine
delivery**, as the most recent target weekday on or before the send — the
"cycle date", not the wall-clock day the mail happened to leave.

The gate in `.github/workflows/weekly-digest.yml` reads it to answer its one
idempotency question: *has this cycle already been delivered?* That is what
lets the retry ladder fire five times on the target day and still send exactly
one email.

Rules:

- Written **only** after a real send. Never on `dry-run`, never on `preview`,
  never by a `--test-email` deliverability check. Writing it on a test run on
  the target day would suppress that day's real digest.
- The catch-up rung stores the **Tuesday** cycle date even when it sends on
  Wednesday, so a repeated catch-up firing is idempotent too.
- Seeded with `2026-10-05`, the last digest actually delivered (by hand, before
  the workflow existed). The four scheduled runs of 2026-10-13 and 2026-10-20
  were discarded by the old wall-clock gate and mailed nothing.

To suppress a cycle by hand, or to repair a send whose marker push failed:

```bash
echo 2026-10-27 > state/last_sent && git commit -am "Marker: 2026-10-27" && git push
```
