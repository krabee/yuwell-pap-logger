"""Coverage for sign-in and for keeping one account's data away from another.

Do not delete: if the user filter on /sessions regresses, one person's therapy
history shows up in another person's account and nothing else in the suite notices.

The app is imported inside a fixture rather than at module scope because main.py
reads settings at import time to configure the session cookie.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_bys, build_record
from tests.test_ingest import FakeWriter


@pytest.fixture
def client(tmp_path, monkeypatch):
    # pydantic-settings looks for .env relative to the working directory, so running
    # the suite from the repository root would pull in the developer's own settings.
    # A value like INIT_USERNAME there silently changes what the app does at boot.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-the-suite")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DEVICE_TZ", "Asia/Bangkok")
    monkeypatch.setenv("INFLUX_URL", "http://localhost:0")
    monkeypatch.setenv("INFLUX_TOKEN", "test-token")

    import app.config

    app.config.get_settings.cache_clear()

    import app.main

    importlib.reload(app.main)

    with TestClient(app.main.app) as test_client:
        # Never talk to a real InfluxDB from the suite.
        test_client.app.state.influx = FakeWriter()
        yield test_client

    app.config.get_settings.cache_clear()


def add_user(client, username: str, password: str = "correct-horse-battery"):
    from app.auth import create_user

    with client.app.state.db_factory() as db:
        return create_user(db, username, password)


def sign_in(client, username: str, password: str = "correct-horse-battery"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )


def boot_app(tmp_path, monkeypatch, db_name="web.db", **extra_env):
    """Start the app against a given database with a given environment.

    Returned as a context manager so a test can boot the same database twice and
    observe what changes between runs.
    """
    import importlib

    # See the note in the `client` fixture: never inherit the repository's .env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / db_name}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-the-suite")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DEVICE_TZ", "Asia/Bangkok")
    monkeypatch.setenv("INFLUX_URL", "http://localhost:0")
    monkeypatch.setenv("INFLUX_TOKEN", "test-token")
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)

    import app.config

    app.config.get_settings.cache_clear()
    import app.main

    importlib.reload(app.main)
    return app.main.app


def test_the_suite_does_not_inherit_the_repositorys_env_file(tmp_path, monkeypatch):
    """Regression guard for a real failure.

    The suite used to read whatever .env sat in the repository root. It looked fine
    for months, then INIT_USERNAME was added to that file and eleven tests started
    booting an app that already had an account — failures with nothing wrong in the
    code under test.
    """
    monkeypatch.chdir(tmp_path)
    for key in ("INIT_USERNAME", "INIT_PASSWORD", "DEVICE_TZ", "SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)

    import app.config

    app.config.get_settings.cache_clear()
    settings = app.config.Settings()

    assert settings.init_username == "", (
        "a value leaked in from a .env outside the test's own directory"
    )
    assert settings.init_password == ""
    app.config.get_settings.cache_clear()


class TestInitAccountFromEnv:
    """INIT_USERNAME/INIT_PASSWORD seed the first account so no token is needed.

    Do not delete: the seeding must happen only while the instance has no accounts.
    If it ever started applying to a running instance it would silently reset or
    duplicate a real account, and a stale value in .env would become a live password.
    """

    CREDS = {"INIT_USERNAME": "owner", "INIT_PASSWORD": "correct-horse-battery"}

    def test_the_account_is_created_at_boot_and_no_token_is_issued(
        self, tmp_path, monkeypatch
    ):
        application = boot_app(tmp_path, monkeypatch, **self.CREDS)

        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            assert client.app.state.setup_token is None, (
                "a setup token was issued even though INIT_* seeded an account"
            )
            response = client.post(
                "/login",
                data={
                    "username": "owner",
                    "password": "correct-horse-battery",
                    "next": "/",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303, "the seeded account cannot sign in"
            assert client.get("/").status_code == 200

        import app.config

        app.config.get_settings.cache_clear()

    def test_the_seeded_account_is_an_administrator(self, tmp_path, monkeypatch):
        application = boot_app(tmp_path, monkeypatch, **self.CREDS)
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            from app.auth import get_user_by_name

            with client.app.state.db_factory() as db:
                assert get_user_by_name(db, "owner").is_admin

        import app.config

        app.config.get_settings.cache_clear()

    def test_setup_page_is_closed_when_the_account_was_seeded(self, tmp_path, monkeypatch):
        application = boot_app(tmp_path, monkeypatch, **self.CREDS)
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            page = client.get("/setup", follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"] == "/login"

        import app.config

        app.config.get_settings.cache_clear()

    def test_a_second_boot_does_not_create_a_duplicate_or_reset_the_password(
        self, tmp_path, monkeypatch
    ):
        """The seeding is one-time. A changed INIT_PASSWORD must not take effect,
        because that would silently undo a password set with app.cli passwd."""
        from app.auth import get_user_by_name
        from app.db import User
        from sqlalchemy import select

        application = boot_app(tmp_path, monkeypatch, **self.CREDS)
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            with client.app.state.db_factory() as db:
                first_hash = get_user_by_name(db, "owner").password_hash

        # Second boot, same database, different password in the environment.
        application = boot_app(
            tmp_path,
            monkeypatch,
            INIT_USERNAME="owner",
            INIT_PASSWORD="a-completely-different-password",
        )
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            with client.app.state.db_factory() as db:
                users = db.execute(select(User)).scalars().all()
                assert len(users) == 1, f"boot created a duplicate account: {users}"
                assert users[0].password_hash == first_hash, (
                    "the password was reset from the environment on a later boot"
                )

            # the original password still works
            assert (
                client.post(
                    "/login",
                    data={
                        "username": "owner",
                        "password": "correct-horse-battery",
                        "next": "/",
                    },
                    follow_redirects=False,
                ).status_code
                == 303
            )

        import app.config

        app.config.get_settings.cache_clear()

    def test_a_short_password_creates_nothing_and_falls_back_to_the_token(
        self, tmp_path, monkeypatch
    ):
        """Better to make the operator deal with /setup than to stand up an
        administrator whose password is four characters long."""
        from app.auth import get_user_by_name

        application = boot_app(
            tmp_path, monkeypatch, INIT_USERNAME="owner", INIT_PASSWORD="short"
        )
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            with client.app.state.db_factory() as db:
                assert get_user_by_name(db, "owner") is None, (
                    "an account was created with a password below the minimum length"
                )
            assert client.app.state.setup_token, "no fallback token was issued"
            assert client.get("/setup").status_code == 200

        import app.config

        app.config.get_settings.cache_clear()

    def test_only_one_of_the_pair_falls_back_to_the_token(self, tmp_path, monkeypatch):
        application = boot_app(tmp_path, monkeypatch, INIT_USERNAME="owner")
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            from app.auth import get_user_by_name

            with client.app.state.db_factory() as db:
                assert get_user_by_name(db, "owner") is None
            assert client.app.state.setup_token

        import app.config

        app.config.get_settings.cache_clear()

    def test_seeding_recovers_an_instance_whose_accounts_were_lost(
        self, tmp_path, monkeypatch
    ):
        """The reason to leave INIT_* in .env: if the database volume disappears
        while InfluxDB still holds the therapy data, the instance comes back owned
        instead of sitting open for whoever reaches it first."""
        application = boot_app(tmp_path, monkeypatch, db_name="lost.db", **self.CREDS)
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            assert client.app.state.setup_token is None

        (tmp_path / "lost.db").unlink()  # the volume is gone

        application = boot_app(tmp_path, monkeypatch, db_name="lost.db", **self.CREDS)
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            assert client.app.state.setup_token is None, (
                "the instance reopened /setup instead of recreating the account"
            )
            from app.auth import get_user_by_name

            with client.app.state.db_factory() as db:
                assert get_user_by_name(db, "owner") is not None

        import app.config

        app.config.get_settings.cache_clear()


class TestSetupTokenFile:
    def test_the_token_survives_a_restart(self, tmp_path, monkeypatch):
        """Regenerating per boot meant the value scrolled away with the log."""
        application = boot_app(tmp_path, monkeypatch, db_name="tok.db")
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            first = client.app.state.setup_token
        assert first

        application = boot_app(tmp_path, monkeypatch, db_name="tok.db")
        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            assert client.app.state.setup_token == first, (
                "a different token was issued on restart"
            )

        import app.config

        app.config.get_settings.cache_clear()

    def test_the_token_is_written_to_disk_and_removed_once_used(
        self, tmp_path, monkeypatch
    ):
        token_file = tmp_path / "setup-token"
        application = boot_app(tmp_path, monkeypatch, db_name="tok2.db")

        with TestClient(application) as client:
            client.app.state.influx = FakeWriter()
            assert token_file.exists(), "the token was not written where the log says"
            assert token_file.read_text().strip() == client.app.state.setup_token

            client.post(
                "/setup",
                data={
                    "setup_token": client.app.state.setup_token,
                    "username": "owner",
                    "password": "correct-horse-battery",
                    "password_repeat": "correct-horse-battery",
                },
                follow_redirects=False,
            )

            assert not token_file.exists(), (
                "the token file outlived the account it was meant to create"
            )

        import app.config

        app.config.get_settings.cache_clear()


class TestFirstRunSetup:
    """The gate that turns a fresh instance into someone's account.

    Do not delete: if either gate fails open, anyone who can reach the port can take
    over the instance — and on a working instance, seize an admin account.
    """

    def test_a_fresh_instance_sends_visitors_to_setup(self, client):
        assert client.get("/", follow_redirects=False).headers["location"].startswith(
            "/login"
        )
        # /login has nothing to offer yet, so it forwards to /setup
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"

    def test_setup_page_is_served_when_no_accounts_exist(self, client):
        response = client.get("/setup")

        assert response.status_code == 200
        assert "setup_token" in response.text

    def test_a_token_is_issued_and_is_not_guessable(self, client):
        token = client.app.state.setup_token

        assert token, "no setup token was issued for an empty instance"
        assert len(token) >= 24, f"token is too short to resist guessing: {len(token)}"

    def test_the_first_account_can_be_created_with_the_token(self, client):
        response = client.post(
            "/setup",
            data={
                "setup_token": client.app.state.setup_token,
                "username": "owner",
                "password": "correct-horse-battery",
                "password_repeat": "correct-horse-battery",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        # and the caller is signed in already
        assert client.get("/").status_code == 200

        from app.auth import get_user_by_name

        with client.app.state.db_factory() as db:
            user = get_user_by_name(db, "owner")
            assert user is not None, "the account was not created"
            assert user.is_admin, "the first account should be an administrator"

    def test_a_wrong_token_is_refused(self, client):
        response = client.post(
            "/setup",
            data={
                "setup_token": "not-the-real-token",
                "username": "intruder",
                "password": "correct-horse-battery",
                "password_repeat": "correct-horse-battery",
            },
        )

        assert response.status_code == 403
        from app.auth import get_user_by_name

        with client.app.state.db_factory() as db:
            assert get_user_by_name(db, "intruder") is None, (
                "an account was created despite the token being wrong"
            )

    def test_setup_closes_permanently_after_the_first_account(self, client):
        """The window must not reopen — otherwise a second visitor could create
        another admin on an instance that already belongs to someone."""
        client.post(
            "/setup",
            data={
                "setup_token": client.app.state.setup_token,
                "username": "owner",
                "password": "correct-horse-battery",
                "password_repeat": "correct-horse-battery",
            },
            follow_redirects=False,
        )
        client.post("/logout", follow_redirects=False)

        page = client.get("/setup", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login"

        # even replaying the original token
        assert client.app.state.setup_token is None

    def test_setup_is_refused_once_an_account_exists_even_with_a_live_token(
        self, client
    ):
        """Covers the race: an account arrives from the CLI while /setup is open."""
        add_user(client, "alice")
        assert client.app.state.setup_token, "precondition: a token was issued at boot"

        response = client.post(
            "/setup",
            data={
                "setup_token": client.app.state.setup_token,
                "username": "intruder",
                "password": "correct-horse-battery",
                "password_repeat": "correct-horse-battery",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        from app.auth import get_user_by_name

        with client.app.state.db_factory() as db:
            assert get_user_by_name(db, "intruder") is None

    def test_short_and_mismatched_passwords_are_refused(self, client):
        token = client.app.state.setup_token

        short = client.post(
            "/setup",
            data={
                "setup_token": token,
                "username": "owner",
                "password": "short",
                "password_repeat": "short",
            },
        )
        assert short.status_code == 400

        mismatch = client.post(
            "/setup",
            data={
                "setup_token": token,
                "username": "owner",
                "password": "correct-horse-battery",
                "password_repeat": "different-password-here",
            },
        )
        assert mismatch.status_code == 400

        from app.auth import get_user_by_name

        with client.app.state.db_factory() as db:
            assert get_user_by_name(db, "owner") is None

    def test_no_token_is_issued_when_accounts_already_exist(self, tmp_path, monkeypatch):
        """A running instance must not mint a token that could seize an admin account."""
        application = boot_app(tmp_path, monkeypatch, db_name="existing.db")

        # First boot: empty, so a token appears and an account is made.
        with TestClient(application) as first:
            first.app.state.influx = FakeWriter()
            assert first.app.state.setup_token
            add_user(first, "alice")

        # Second boot against the same database: the instance is already owned.
        application = boot_app(tmp_path, monkeypatch, db_name="existing.db")
        with TestClient(application) as second:
            second.app.state.influx = FakeWriter()
            assert second.app.state.setup_token is None, (
                "a setup token was issued on an instance that already has accounts"
            )

        import app.config

        app.config.get_settings.cache_clear()


class TestSignIn:
    def test_anonymous_visitor_is_sent_to_the_login_page(self, client):
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_sessions_page_also_requires_an_account(self, client):
        response = client.get("/sessions", follow_redirects=False)

        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_correct_credentials_sign_in(self, client):
        add_user(client, "alice")

        response = sign_in(client, "alice")

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert client.get("/").status_code == 200

    def test_wrong_password_is_refused(self, client):
        add_user(client, "alice")

        response = sign_in(client, "alice", "wrong-password")

        assert response.status_code == 401
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_unknown_user_is_refused(self, client):
        response = sign_in(client, "nobody")

        assert response.status_code == 401

    def test_sign_out_ends_the_session(self, client):
        add_user(client, "alice")
        sign_in(client, "alice")
        assert client.get("/").status_code == 200

        client.post("/logout", follow_redirects=False)

        assert client.get("/", follow_redirects=False).status_code == 303

    def test_redirect_target_cannot_leave_the_site(self, client):
        """An open redirect here would let a phishing link bounce off the login page."""
        add_user(client, "alice")

        response = client.post(
            "/login",
            data={
                "username": "alice",
                "password": "correct-horse-battery",
                "next": "//evil.example.com/",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_health_needs_no_account(self, client):
        response = client.get("/health")

        # Degraded is expected: the suite points at an InfluxDB that is not running.
        assert response.status_code in (200, 503)
        assert "influx" in response.json()


class TestPerAccountIsolation:
    def _import_night(self, client, username, serial, start):
        """Import one session directly through the pipeline, as the web upload would."""
        from app.auth import get_user_by_name
        from app.ingest import Ingestor

        data = build_bys(
            [build_record(pressure=6.0) for _ in range(45)],
            start=start,
            serial=serial,
        )
        with client.app.state.db_factory() as db:
            user = get_user_by_name(db, username)
            ingestor = Ingestor(
                db=db,
                user=user,
                settings=client.app.state.settings,
                writer=FakeWriter(),
            )
            results = list(ingestor.ingest_files([("00100001.BYS", data)]))
        assert results[0].status == "imported", results[0].reason
        return results[0]

    def test_each_account_sees_only_its_own_sessions(self, client):
        add_user(client, "alice")
        add_user(client, "bob")

        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 21, 22, 0, 0)
        )
        self._import_night(
            client, "bob", "YH550A-BBBBBBBBB", datetime(2025, 8, 22, 23, 0, 0)
        )

        sign_in(client, "alice")
        alice_page = client.get("/sessions").text

        assert "YH550A-AAAAAAAAA" in alice_page, "alice cannot see her own machine"
        assert "YH550A-BBBBBBBBB" not in alice_page, (
            "bob's machine serial leaked into alice's session list"
        )

        client.post("/logout")
        sign_in(client, "bob")
        bob_page = client.get("/sessions").text

        assert "YH550A-BBBBBBBBB" in bob_page
        assert "YH550A-AAAAAAAAA" not in bob_page, (
            "alice's machine serial leaked into bob's session list"
        )

    def _import_many(self, client, username, serial, count):
        from app.auth import get_user_by_name
        from app.db import Upload
        from datetime import timedelta, timezone

        with client.app.state.db_factory() as db:
            user = get_user_by_name(db, username)
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for i in range(count):
                start = base + timedelta(hours=i * 8)
                db.add(
                    Upload(
                        user_id=user.id,
                        file_sha256=f"hash-{i:05d}",
                        filename=f"{i:08d}.BYS",
                        serial=serial,
                        model="YH550",
                        session_id=f"s{i}",
                        session_start=start,
                        night_date=(start - timedelta(hours=12)).date().isoformat(),
                        duration_minutes=120,
                        ahi=1.0,
                        avg_pressure=8.0,
                        p90_pressure=9.0,
                        avg_leak=0.5,
                        points_written=121,
                    )
                )
            db.commit()

    def test_every_session_is_reachable_through_paging(self, client):
        """The page used to cap at 500 rows with no way further back, so a third of
        one user's history simply could not be opened. Every row must be reachable."""
        add_user(client, "alice")
        self._import_many(client, "alice", "YH550A-AAAAAAAAA", 120)
        sign_in(client, "alice")

        seen = set()
        page = 1
        while page <= 20:  # guard against a paging loop
            body = client.get(f"/sessions?page={page}").text
            found = set(re.findall(r"(\d{8})\.BYS", body))
            assert found, f"page {page} rendered no session rows"
            seen |= found
            if 'href="/sessions?page=%d"' % (page + 1) not in body:
                break
            page += 1

        assert len(seen) == 120, (
            f"only {len(seen)} of 120 sessions were reachable by paging to the end"
        )

    def test_first_page_shows_the_newest_sessions(self, client):
        add_user(client, "alice")
        self._import_many(client, "alice", "YH550A-AAAAAAAAA", 120)
        sign_in(client, "alice")

        body = client.get("/sessions").text

        assert "00000119.BYS" in body, "newest session is not on the first page"
        assert "00000000.BYS" not in body, "oldest session should not be on page 1"
        assert "1–50 จาก 120" in body, f"range label missing or wrong"

    def test_out_of_range_pages_are_clamped_not_broken(self, client):
        """Bookmarked page numbers go stale the moment new sessions arrive."""
        add_user(client, "alice")
        self._import_many(client, "alice", "YH550A-AAAAAAAAA", 120)
        sign_in(client, "alice")

        assert client.get("/sessions?page=999").status_code == 200
        assert client.get("/sessions?page=0").status_code == 200
        assert client.get("/sessions?page=-5").status_code == 200
        assert "00000119.BYS" in client.get("/sessions?page=-5").text, (
            "a page below 1 should clamp to the first page"
        )

    def test_times_are_shown_in_the_machines_timezone(self, client):
        """Stored UTC, displayed as the machine showed it.

        A session recorded at 02:38 on the device must read 02:38 on the page. Showing
        the raw UTC 19:38 would put it on the wrong evening and contradict the
        night_date beside it.
        """
        add_user(client, "alice")
        # 02:38 Bangkok on 10 Aug is 19:38 UTC on 9 Aug.
        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2026, 8, 10, 2, 38, 0)
        )

        sign_in(client, "alice")
        page = client.get("/sessions").text

        assert "2026-08-10 02:38" in page, (
            "session start is not rendered in the device timezone; UTC would show 19:38"
        )
        assert "19:38" not in page, "raw UTC leaked into the page"
        assert "Asia/Bangkok" in page, "the page does not say which timezone it is using"

    def test_nights_and_sessions_are_counted_separately(self, client):
        """The machine writes a file per power-on, so a night is usually several
        sessions. Reporting the session count under a "nights" label understates
        hours per night by the same factor — roughly 2.5x on real data.
        """
        add_user(client, "alice")
        # three sessions, two nights: 21 Aug has two, 22 Aug has one
        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 21, 22, 0, 0)
        )
        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 22, 3, 0, 0)
        )
        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 22, 23, 0, 0)
        )

        from app.db import Upload

        with client.app.state.db_factory() as db:
            assert db.query(Upload).count() == 3, "setup failed; assertions below are void"

        sign_in(client, "alice")
        page = client.get("/sessions").text

        # Each import is 45 minutes, so: 2 nights, 3 sessions, 135 min total,
        # 67.5 min per night = 1.1 h.
        stats = {}
        import re

        for label, value in re.findall(
            r'<span class="label">([^<]+)</span><span class="value">([^<]+)</span>', page
        ):
            stats[label] = value

        assert stats["จำนวนคืน"] == "2", f"expected 2 nights, page shows {stats}"
        assert stats["จำนวน session"] == "3", f"expected 3 sessions, page shows {stats}"
        assert stats["ชั่วโมงเฉลี่ย/คืน"] == "1.1", (
            f"hours per night should total the sessions in each night, page shows {stats}"
        )
        assert stats["ชั่วโมงรวม"] == "2.2", f"page shows {stats}"

    def test_average_ahi_is_weighted_by_session_length(self, client):
        """A short session must not pull the average as hard as a long one."""
        from app.auth import get_user_by_name
        from app.db import Upload
        from datetime import timezone

        add_user(client, "alice")
        with client.app.state.db_factory() as db:
            alice = get_user_by_name(db, "alice")
            # 600 minutes at AHI 1.0 and 60 minutes at AHI 11.0.
            # Unweighted mean would be 6.0; weighted is (600*1 + 60*11)/660 = 1.909.
            for i, (minutes, ahi) in enumerate([(600, 1.0), (60, 11.0)]):
                db.add(
                    Upload(
                        user_id=alice.id,
                        file_sha256=f"hash{i}",
                        filename=f"{i}.BYS",
                        serial="YH550A-AAAAAAAAA",
                        model="YH550",
                        session_id=f"s{i}",
                        session_start=datetime(2025, 8, 21, 22, 0, 0, tzinfo=timezone.utc),
                        night_date="2025-08-21",
                        duration_minutes=minutes,
                        ahi=ahi,
                        avg_pressure=8.0,
                        p90_pressure=9.0,
                        avg_leak=1.0,
                        points_written=minutes + 1,
                    )
                )
            db.commit()

        sign_in(client, "alice")
        page = client.get("/sessions").text

        assert "1.9" in page, (
            "expected the duration-weighted AHI of 1.9; an unweighted mean would show 6.0"
        )
        assert ">6.0<" not in page, "AHI appears to be an unweighted mean of sessions"

    def test_totals_are_per_account(self, client):
        add_user(client, "alice")
        add_user(client, "bob")

        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 21, 22, 0, 0)
        )
        self._import_night(
            client, "alice", "YH550A-AAAAAAAAA", datetime(2025, 8, 22, 22, 0, 0)
        )
        self._import_night(
            client, "bob", "YH550A-BBBBBBBBB", datetime(2025, 8, 23, 22, 0, 0)
        )

        sign_in(client, "bob")
        page = client.get("/sessions").text

        # Bob has exactly one night; alice's two must not be counted into his totals.
        assert page.count("YH550A-BBBBBBBBB") == 1, (
            "bob should see exactly one session row"
        )
        assert "YH550A-AAAAAAAAA" not in page

        from app.auth import get_user_by_name
        from app.db import Upload

        with client.app.state.db_factory() as db:
            bob = get_user_by_name(db, "bob")
            assert db.query(Upload).filter_by(user_id=bob.id).count() == 1
            assert db.query(Upload).count() == 3, (
                "setup did not import all three nights; the assertions above prove little"
            )


class TestPasswordStorage:
    def test_passwords_are_stored_as_argon2_hashes(self, client):
        """A fast hash would let anyone with the database file crack these offline."""
        user = add_user(client, "alice", "correct-horse-battery")

        assert user.password_hash.startswith("$argon2"), user.password_hash[:20]
        assert "correct-horse-battery" not in user.password_hash
