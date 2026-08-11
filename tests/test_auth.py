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
        import importlib

        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'existing.db'}")
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-the-suite")
        monkeypatch.setenv("INFLUX_URL", "http://localhost:0")
        monkeypatch.setenv("INFLUX_TOKEN", "test-token")

        import app.config

        app.config.get_settings.cache_clear()
        import app.main

        importlib.reload(app.main)

        # First boot: empty, so a token appears and an account is made.
        with TestClient(app.main.app) as first:
            first.app.state.influx = FakeWriter()
            assert first.app.state.setup_token
            add_user(first, "alice")

        # Second boot against the same database: the instance is already owned.
        with TestClient(app.main.app) as second:
            second.app.state.influx = FakeWriter()
            assert second.app.state.setup_token is None, (
                "a setup token was issued on an instance that already has accounts"
            )

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
        hours per night by the same factor — on real data it understated them by roughly 2.5x.
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
