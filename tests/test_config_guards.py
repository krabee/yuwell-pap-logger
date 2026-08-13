"""Boot-time refusal on a public/default SECRET_KEY.

SECRET_KEY signs the session cookie. A deployment still running on the Settings
default or a past .env.example placeholder is signing cookies with a value anyone
who has ever seen this public repo can read — they can forge `user_id=1` and read
another account's therapy data. There is no user-visible symptom until it is
exploited, so this file is the only thing that catches a regression here.

Do not delete: if `_require_a_real_secret_key` regresses (or its call in
`lifespan` moves after `create_db_engine`), nothing else in the suite notices —
the app would just boot normally on a key an attacker can read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import INSECURE_SECRET_KEYS
from tests.test_auth import boot_app
from tests.test_ingest import FakeWriter


def test_empty_secret_key_refuses_to_boot(tmp_path, monkeypatch):
    application = boot_app(tmp_path, monkeypatch, SECRET_KEY="")

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(application):
            pass


def test_whitespace_only_secret_key_refuses_to_boot(tmp_path, monkeypatch):
    application = boot_app(tmp_path, monkeypatch, SECRET_KEY="   ")

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(application):
            pass


@pytest.mark.parametrize("insecure_key", sorted(INSECURE_SECRET_KEYS))
def test_every_known_insecure_secret_key_refuses_to_boot(tmp_path, monkeypatch, insecure_key):
    """Parametrized off INSECURE_SECRET_KEYS itself, not hardcoded, so adding a
    third value to the set is covered automatically without touching this test."""
    application = boot_app(tmp_path, monkeypatch, SECRET_KEY=insecure_key)

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(application):
            pass


def test_both_keys_this_repo_ever_shipped_stay_in_the_set():
    """Hardcoded on purpose, not derived from INSECURE_SECRET_KEYS.

    test_every_known_insecure_secret_key_refuses_to_boot above parametrizes off
    INSECURE_SECRET_KEYS itself, so removing a value from the set deletes that
    test's own coverage for it silently — nothing goes red. These two assertions
    do not depend on the set's current contents, so a shrunk set fails here.
    """
    assert "dev-only-insecure-key-change-me" in INSECURE_SECRET_KEYS, (
        "this was Settings.secret_key's own default across every commit in this "
        "repo's history, now public — removing it from INSECURE_SECRET_KEYS lets an "
        "instance still running on it boot again, and anyone who has ever read the "
        "source can forge a session cookie as user_id=1 and read another account's "
        "therapy data. Do not remove this assertion to make a shrunk set pass."
    )
    assert "change-me-to-a-long-random-string" in INSECURE_SECRET_KEYS, (
        "this was .env.example's own placeholder across every commit in this "
        "repo's history, now public — removing it from INSECURE_SECRET_KEYS lets an "
        "instance still running on it boot again, and anyone who has ever read the "
        "source can forge a session cookie as user_id=1 and read another account's "
        "therapy data. Do not remove this assertion to make a shrunk set pass."
    )


def test_docker_compose_requires_secret_key_at_the_compose_layer():
    """Regression guard for docker-compose.yml itself, owned by the engineer role.

    app.main's own RuntimeError still catches a missing SECRET_KEY, but without
    ${SECRET_KEY:?...} on the app service here, `docker compose up` starts the
    container anyway with SECRET_KEY unset, and it crash-loops on the app-level
    guard (restart: unless-stopped) instead of `docker compose config` failing
    fast with a clear message before anything starts.

    Parses the file and reads services.app.environment.SECRET_KEY directly rather
    than checking whether the string appears anywhere in the file: a plain
    substring check still passes if the real line is commented out (e.g. left
    behind from debugging as `# TODO re-enable: ${SECRET_KEY:?...}`) even though
    the compose layer no longer requires anything at that point.
    """
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())

    app_environment = compose["services"]["app"].get("environment") or {}
    secret_key_value = app_environment.get("SECRET_KEY", "")
    assert "${SECRET_KEY:?" in secret_key_value, (
        f"services.app.environment.SECRET_KEY in docker-compose.yml is "
        f"{secret_key_value!r} — it must use ${{SECRET_KEY:?...}} so `docker compose "
        f"up` refuses to start the app container with SECRET_KEY unset, instead of "
        f"letting it crash-loop on the app-level guard (restart: unless-stopped)"
    )


def test_a_real_secret_key_boots_normally(tmp_path, monkeypatch):
    """Proves the guard isn't refusing everything — only the known-bad values."""
    application = boot_app(
        tmp_path, monkeypatch, SECRET_KEY="a-genuinely-random-looking-key-a1b2c3d4e5"
    )

    with TestClient(application) as client:
        client.app.state.influx = FakeWriter()
        response = client.get("/health")
        assert response.status_code in (200, 503), (
            f"app failed to boot on a real secret key: {response.status_code}"
        )


def test_the_guard_runs_before_the_database_file_is_created(tmp_path, monkeypatch):
    """The check must sit before create_db_engine/init_db, or a rejected boot still
    leaves a stray sqlite file behind. This is the test that proves ordering, not
    just that a RuntimeError happens somewhere."""
    db_path = tmp_path / "guarded.db"
    application = boot_app(tmp_path, monkeypatch, db_name="guarded.db", SECRET_KEY="")

    assert not db_path.exists(), "setup created the db file before boot even ran"

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(application):
            pass

    assert not db_path.exists(), (
        "the sqlite file was created even though boot was refused — the SECRET_KEY "
        "check must run before create_db_engine, not just somewhere in lifespan"
    )


def test_env_example_does_not_ship_a_placeholder_secret_key():
    """Regression guard for the file .env.example itself, owned by the docs role.

    docker-compose.yml uses ${SECRET_KEY:?...}, which only fails the compose run when
    the variable is unset or empty. If .env.example ever ships a non-empty
    placeholder again, `cp .env.example .env` produces a value that satisfies `:?`
    fine and only app.main's own guard would still catch it. This test exists so
    that a regression here fails loudly instead of relying on that second layer.
    """
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    lines = env_example.read_text().splitlines()

    secret_key_lines = [
        line
        for line in lines
        if line.split("=", 1)[0] == "SECRET_KEY" and not line.lstrip().startswith("#")
    ]
    assert len(secret_key_lines) == 1, (
        f"expected exactly one uncommented SECRET_KEY= line in .env.example, "
        f"found {secret_key_lines!r}"
    )

    value = secret_key_lines[0].split("=", 1)[1].strip()
    assert value == "", (
        f".env.example SECRET_KEY is not blank (got {value!r}). "
        f"${{SECRET_KEY:?}} in docker-compose.yml treats any non-empty value as set, "
        f"so a placeholder here means `cp .env.example .env` produces an instance "
        f"docker compose is willing to start with a publicly-known key."
    )
    assert value not in INSECURE_SECRET_KEYS, (
        f".env.example SECRET_KEY {value!r} is one of the values app.main already "
        f"refuses to boot on — pick a genuinely blank value instead"
    )
