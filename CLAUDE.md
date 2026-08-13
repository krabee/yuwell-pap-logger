# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted web app that reads `.BYS` session files off the SD card of a **Yuwell
YH550 (BreathCare ECO)** CPAP machine and stores them in InfluxDB indefinitely. The
machine's own card fills up and gets overwritten; this exists so the history survives.

The output is charts a person may take to a doctor. **A number that is wrong but
plausible is the worst possible failure here** — prefer refusing to import over
guessing.

## Commands

```bash
uv sync --extra dev                       # set up
uv run pytest                             # full suite
uv run pytest tests/test_yh550_parser.py -v
uv run pytest -m realdata                 # only the tests that need real .BYS files
uv run pytest -k dedupe                   # single behaviour
uv run uvicorn app.main:app --reload      # dev server on :8000

uv run python -m app.cli adduser <name>   # accounts (password is prompted, never argv)
uv run python -m app.cli import <name> <path>...   # import without a browser

docker compose --profile local-influx up -d   # full stack incl. InfluxDB
docker compose up -d                          # app + Grafana against an external InfluxDB
```

There is no linter configured. Match the surrounding style.

## Architecture

```
browser ──login/upload──▶ FastAPI ──▶ parsers ──▶ metrics ──▶ InfluxDB   ◀── Grafana
                             │                                   ▲
                             └──▶ SQLAlchemy ──▶ SQLite          └── local or external
                                  users, devices, ingest ledger
```

The split that matters: **InfluxDB holds the therapy data, SQLite holds only what
Influx answers badly** — who owns which machine, and whether a file was already
imported.

`app/ingest.py` is the spine. It is a generator yielding one `IngestResult` per file,
which lets `main.py` stream progress as server-sent events and lets `cli.py` reuse the
identical path. It commits the ledger **per file**, and that single decision is what
makes an interrupted 800-file upload resumable without a job queue or resume button:
re-uploading the whole batch skips what already landed.

Reading order for a newcomer: `app/parsers/base.py` (the contract) →
`app/parsers/yh550.py` (the format, documented in its docstring) → `app/ingest.py`.

## Facts about the file format

Verified against 46 real session files, not taken from documentation:

- `len(data) == 51 + record_count * 10 + 1`. The trailing byte is a `0xFA` footer that
  the upstream README omits; it only appears in the 010-editor grammar.
- `data[50] == 0xF9` marks the end of the header.
- `end - start == record_count` minutes, exactly. Each record's timestamp is
  `start + n minutes`; there is nothing to infer.
- Decimals are stored as value × 10. Years are two digits. Little endian.

The full offset table lives in the docstring of `app/parsers/yh550.py` and is the
single source for it — other documents link there rather than restating the numbers.

## Constraints, and why they exist

These are the things that look like improvements and are not.

- **Never raise on non-zero values in log-record bytes 1, 2, 6, 7, 8.** Their meaning
  is unknown, and they genuinely carry data — real files show a 1 walking through them
  across the last records of a session. The upstream reference implementation raises
  here and therefore crashes on its own sample data. Collect them into
  `SessionFile.warnings` and store the raw values. A proposal to "tighten validation"
  on these bytes should be rejected. (verified 2026-08-10)

- **Trailing all-`0xFF` records are erased flash, not data — discard them.** A session
  the device never finished writing still declares its reserved record count in the
  header, and everything past the last real write reads back as `0xFF`. Taken
  literally that decodes as 25.5 cmH₂O and 255 events per minute. On a real card, one
  file (`00200126.BYS`, 363 declared / 200 written) produced an AHI of **20,611**
  before this was handled; after trimming, the computed average pressure matched what
  the machine itself reported to within 0.05 cmH₂O — the cross-check that proves the
  trim is correct rather than just plausible. `_count_written_records` trims only a
  *trailing* run, plus one torn boundary record — a gap in the middle is left in place
  and warned about, because that would mean something other than an interrupted write.
  (found in real data 2026-08-10)

- **A night is not a session — aggregate before you report.** The machine writes a new
  file every time it is switched on, so a night is routinely 2–4 sessions (measured
  against a real year of data). Anything labelled "per night" must group by the
  `night_date` tag first. Counting `pap_session` points gives sessions instead of
  nights; the first version of the dashboard did that and, over a 90-day window,
  reported more nights than there were calendar days in the window, with hours per
  night understated by roughly 2.5×.
  Averages have the same trap: a 15-minute session must not weigh as much as a
  5-hour one. AHI per night is `sum(events) / sum(minutes) * 60`, and average
  pressure/leak are duration-weighted. On the SQL side,
  `sum(ahi * duration) / sum(duration)` is exactly the same quantity and needs no
  extra columns. (found from a user's screenshot 2026-08-10)

  Two Flux gotchas that cost a debugging round: `pivot` refuses to mix integer and
  float value types, so cast with `float()` **before** pivoting whenever
  `duration_min` appears alongside a float field; and `group()` is required before
  `pivot` in table panels, or Grafana renders one table per tag value.

- **Timestamps in the file are naive device-local wall-clock.** Conversion to UTC
  happens once, in `app/influx.py`, using `DEVICE_TZ`. No `utcnow()` anywhere near
  parsing. A session starting 00:42 belongs to the **previous** night —
  `metrics.night_date` shifts by 12 hours to express that.

- **Deduplicate on `sha256` of the file contents, never on the filename.** The device
  restarts numbering at `00100001.BYS` after a reset, so names repeat across different
  nights. Hash before parsing: on a re-uploaded card almost every file is skipped
  without being decoded.

- **Everything configurable goes through `app/config.py`.** The user must be able to
  point at a different InfluxDB by editing `.env` alone — that is a stated requirement,
  not a nicety. The local InfluxDB lives behind the `local-influx` compose profile for
  the same reason.

- **All database access goes through SQLAlchemy and `DATABASE_URL`.** SQLite is the
  current default but the move to Postgres/MariaDB is expected, so no `sqlite3`
  calls and no dialect-specific SQL. (decided 2026-08-10)

- **Bucket retention must stay infinite.** Long-term storage is the entire point. A
  year of one person's data is roughly 200k points.

- **No job queue.** SSE plus per-file ledger commits was chosen deliberately over
  Celery/RQ and a jobs table. (decided 2026-08-10)

- **Argon2 for passwords.** The first account arrives one of two ways, decided once
  per boot in `_bootstrap_access` and only ever while the instance has zero accounts:

  1. `INIT_USERNAME` + `INIT_PASSWORD` in the environment seed an admin at startup —
     no token, no `/setup`. **One-time only**: a later boot never updates or
     duplicates the account, because doing so would silently undo a password set with
     `app.cli passwd`. When the values go stale the app says so in the log rather than
     letting `.env` read as the current password. Leaving them in place is deliberate
     — if the database volume is lost while InfluxDB still holds the therapy data, the
     instance comes back owned instead of open to whoever reaches the port first.
  2. Otherwise a setup token is generated, stored at `DATA_DIR/setup-token` and
     printed to the log. It survives restarts (regenerating per boot meant the value
     scrolled away) and is deleted the moment an account exists.

  `/setup` re-checks both gates on every POST. Later accounts come from the CLI; there
  is no open sign-up. Covered by `TestInitAccountFromEnv`, `TestSetupTokenFile` and
  `TestFirstRunSetup`, all mutation-tested. (added 2026-08-10, reworked 2026-08-12)

- **The app configures its own logging.** uvicorn sets up only its own loggers and
  leaves the root logger at WARNING, so `log.info` from this package goes nowhere.
  That hid the line naming the account just created — the single most important
  message in the first-run flow. `logging.basicConfig` at import of `app.main` fixes
  it; `LOG_LEVEL` controls it. (found 2026-08-12)

- **Uploaded archives are untrusted.** `extract_bys_from_zip` checks entry names,
  entry count and total expanded size before reading anything.

- **`SECRET_KEY` set to a default or placeholder value refuses to boot — it does not
  just log a warning.** It signs the session cookie; a key this repo has ever shipped
  (as the `Settings` default or a past `.env.example` placeholder) is public knowledge,
  so anyone who has seen the source can forge `user_id=1` and read another account's
  therapy data. "Warn and continue" was rejected: a log line nobody reads doesn't stop
  that, and it isn't worth spinning up `uv run uvicorn` just to read one. The check
  does not branch on `APP_BASE_URL` to guess dev vs. prod either — that is a heuristic
  that fails exactly when it matters, since a real deployment behind a reverse proxy
  routinely has `APP_BASE_URL` set to `http://`. `.env.example` ships `SECRET_KEY=`
  **empty**, not a placeholder, because `${SECRET_KEY:?}` in compose only catches unset
  or empty — a placeholder string sails right through it, and copying `.env.example` to
  `.env` untouched is the single most common way this is triggered. `Settings.secret_key`
  keeps its insecure default and has no validator on the class itself, because
  `Settings()` must still be constructible without any environment for tests that build
  it directly; the refusal happens once, at boot, in `app.main.lifespan` before
  `create_db_engine` runs. (added 2026-08-12)

- **Grafana is not a security boundary.** Per-account separation is real on the FastAPI
  pages and only cosmetic in Grafana's `$user` variable. The README says so explicitly;
  do not let that wording get softened or dropped.

- **The UI reports numbers, never interpretations.** No treatment advice, no "your
  therapy is working" text.

## Data model in InfluxDB

`pap_minute` — one point per minute. Tags `user`, `serial`, `session_id`, `mode`.
Fields include the undocumented bytes (`u1`, `u2`, `u6`, `u7`, `u8`) on purpose: if
they are ever decoded, it can be applied to data already collected instead of asking
users to re-import.

`pap_session` — one point per session at its start. Adds the `night_date` tag and all
the summary fields, plus `device_avg_pressure`/`device_avg_leak` — the machine's own
numbers, kept so a scaling error in our maths can be caught after the fact.

Cardinality is about one session per night, so roughly 3,650 series per user per
decade. `night_date` correlates 1:1 with `session_id`, so it costs nothing.

## Tests

`tests/conftest.py:build_bys` assembles session files byte by byte. This is what lets
the suite run on a machine that has no patient data. Real `.BYS` files are gitignored —
they contain a device serial and health data — so tests needing them are marked
`realdata` and skip when `tests/fixtures/` is empty.

The `realdata` cross-check compares our computed averages against the machine's own
summary bytes. That is the check that would catch a systematic scaling error which
every structural test still passes.

When adding a test file, add it to the table in `.claude/agents/qa.md` in the **same**
round, with the reason it must not be deleted. Skipping that is how the role files
drift out of date.

Tests must never inherit the repository's `.env`. pydantic-settings resolves it
relative to the working directory, so the fixtures `monkeypatch.chdir(tmp_path)`.
This was not theoretical: adding `INIT_USERNAME` to a developer's own `.env` made
eleven tests fail with nothing wrong in the code under test.
`test_the_suite_does_not_inherit_the_repositorys_env_file` guards it.

## Agent team

`.claude/agents/*.md` are filled in for this project, not skeletons — they carry the
constraints above with their reasons. `./orchestrator.sh` starts a five-pane tmux team
(see [agents-team](https://github.com/pragmaTH/agents-team)). Worth it for work that
spans several rounds; for a single change, one session is cheaper.

## Not supported

Only YH550. The YH580, YH830 and YH680/YH690 use different layouts and there is no
test data for them, so `registry.parse` refuses anything that does not match the YH550
structure rather than guessing. Adding a model means a new module in `app/parsers/`
plus an entry in `_PARSERS`; nothing downstream changes.

## Attribution

The binary format comes from [Centurix/djmed](https://github.com/Centurix/djmed) by
Chris Read. That repository has **no LICENSE file**, so this parser was written from
the published field tables rather than derived from its code. Keep it that way, and
keep the credit in both `README.md` and the page footer.
