"""Coverage for deduplication, archive safety and device ownership.

Do not delete: this file pins the rules that a re-uploaded SD card is cheap, that a
hostile zip is refused, and that one account cannot import another account's machine.

InfluxDB is mocked at the writer boundary — the parser, metrics, ledger and ownership
logic all run for real.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import pytest

from app.config import Settings
from app.db import Device, Upload, create_db_engine, create_session_factory, init_db
from app.influx import InfluxUnavailable
from app.ingest import Ingestor, ZipRejected, extract_bys_from_zip, sha256_of
from tests.conftest import TEST_SERIAL, build_bys, build_record


class FakeWriter:
    """Stands in for InfluxWriter. Records what it was asked to write."""

    def __init__(self, fail: bool = False):
        self.sessions = []
        self.fail = fail

    def write_session(self, session, summary, username, source_file, file_sha256):
        if self.fail:
            raise InfluxUnavailable("influx is down (simulated)")
        self.sessions.append((session, summary, username, source_file, file_sha256))
        return len(session.records) + 1  # minute points plus the session point

    def close(self):
        pass

    def health(self) -> dict:
        """Mirrors InfluxWriter.health so /health can be exercised without a server."""
        return {
            "url": "fake://influx",
            "org": "test",
            "bucket": "test",
            "reachable": not self.fail,
            "bucket_found": not self.fail,
            "retention": "infinite",
            "error": "simulated failure" if self.fail else None,
        }

    @property
    def points_written(self):
        return sum(len(s[0].records) + 1 for s in self.sessions)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key="test-key",
        device_tz="Asia/Bangkok",
        influx_url="http://localhost:0",
        influx_token="test",
    )


@pytest.fixture
def db(settings):
    engine = create_db_engine(settings)
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def users(db):
    from app.auth import create_user

    alice = create_user(db, "alice", "correct-horse-battery")
    bob = create_user(db, "bob", "correct-horse-battery")
    return alice, bob


@pytest.fixture
def sample_bys():
    records = [build_record(pressure=5.0 + (i % 3), leak=1.0) for i in range(60)]
    records[10] = build_record(oai=1)
    records[20] = build_record(hi=2)
    records[30] = build_record(cai=1)
    return build_bys(records, start=datetime(2025, 8, 21, 22, 30, 0))


def make_ingestor(db, user, settings, writer=None):
    return Ingestor(db=db, user=user, settings=settings, writer=writer or FakeWriter())


class TestImport:
    def test_imports_a_session_and_records_it_in_the_ledger(
        self, db, users, settings, sample_bys
    ):
        alice, _ = users
        writer = FakeWriter()
        ingestor = make_ingestor(db, alice, settings, writer)

        results = list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))

        assert len(results) == 1, "expected exactly one result for one file"
        result = results[0]
        assert result.status == "imported", result.reason
        assert result.minutes == 60
        assert result.ahi == pytest.approx(4.0)
        assert result.night_date == "2025-08-21"

        rows = db.query(Upload).all()
        assert len(rows) == 1, "ledger row was not written; a rerun would reimport"
        assert rows[0].file_sha256 == sha256_of(sample_bys)
        assert rows[0].points_written == 61

    def test_rejects_a_file_that_is_not_a_session(self, db, users, settings):
        alice, _ = users
        ingestor = make_ingestor(db, alice, settings)

        results = list(ingestor.ingest_files([("notes.txt", b"hello")]))

        assert len(results) == 1
        assert results[0].status == "failed"
        assert "not a .bys" in results[0].reason

    def test_corrupt_bys_fails_without_writing_a_ledger_row(self, db, users, settings):
        alice, _ = users
        ingestor = make_ingestor(db, alice, settings)

        results = list(ingestor.ingest_files([("broken.BYS", b"\x00" * 200)]))

        assert results[0].status == "failed"
        assert db.query(Upload).count() == 0

    def test_influx_failure_leaves_no_ledger_row_so_the_file_is_retried(
        self, db, users, settings, sample_bys
    ):
        """A file recorded as done while its data never landed would be lost silently."""
        alice, _ = users
        ingestor = make_ingestor(db, alice, settings, FakeWriter(fail=True))

        results = list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))

        assert results[0].status == "failed"
        assert "influx" in results[0].reason.lower()
        assert db.query(Upload).count() == 0, (
            "a ledger row was written despite the write failing — the file would be "
            "skipped on retry and its data lost"
        )


class TestDeduplication:
    def test_second_upload_of_the_same_bytes_is_skipped(
        self, db, users, settings, sample_bys
    ):
        alice, _ = users
        writer = FakeWriter()
        ingestor = make_ingestor(db, alice, settings, writer)

        first = list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))
        second = list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))

        assert first[0].status == "imported"
        assert second[0].status == "duplicate", second[0].reason
        assert len(writer.sessions) == 1, "duplicate was parsed and written again"
        assert db.query(Upload).count() == 1

    def test_dedupe_follows_content_not_filename(self, db, users, settings, sample_bys):
        """The device restarts numbering after a reset, so names repeat across nights."""
        alice, _ = users
        writer = FakeWriter()
        ingestor = make_ingestor(db, alice, settings, writer)

        other_night = build_bys(
            [build_record() for _ in range(30)], start=datetime(2025, 9, 1, 23, 0, 0)
        )

        list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))
        results = list(ingestor.ingest_files([("00100001.BYS", other_night)]))

        assert results[0].status == "imported", (
            "a different night reusing the same filename was treated as a duplicate"
        )
        assert len(writer.sessions) == 2

    def test_same_file_under_a_different_name_is_still_a_duplicate(
        self, db, users, settings, sample_bys
    ):
        alice, _ = users
        ingestor = make_ingestor(db, alice, settings)

        list(ingestor.ingest_files([("00100001.BYS", sample_bys)]))
        results = list(ingestor.ingest_files([("copy-of-00100001.BYS", sample_bys)]))

        assert results[0].status == "duplicate"

    def test_dedupe_is_per_account(self, db, users, settings, sample_bys):
        """Bob importing his own copy must not be blocked by Alice's ledger."""
        alice, bob = users
        list(make_ingestor(db, alice, settings).ingest_files([("a.BYS", sample_bys)]))

        # Bob owns a different machine, so give him a file with another serial.
        bobs_file = build_bys(
            [build_record() for _ in range(10)], serial="YH550A-BBBBBBBBB"
        )
        results = list(make_ingestor(db, bob, settings).ingest_files([("b.BYS", bobs_file)]))

        assert results[0].status == "imported", results[0].reason


class TestDeviceOwnership:
    def test_first_uploader_claims_the_machine(self, db, users, settings, sample_bys):
        alice, _ = users
        list(make_ingestor(db, alice, settings).ingest_files([("a.BYS", sample_bys)]))

        device = db.query(Device).filter_by(serial=TEST_SERIAL).one()
        assert device.user_id == alice.id

    def test_another_account_cannot_import_that_machine(
        self, db, users, settings, sample_bys
    ):
        """Silently merging two people's therapy data would be far worse than refusing."""
        alice, bob = users
        list(make_ingestor(db, alice, settings).ingest_files([("a.BYS", sample_bys)]))

        other_night = build_bys(
            [build_record() for _ in range(20)],
            start=datetime(2025, 9, 2, 22, 0, 0),
            serial=TEST_SERIAL,
        )
        results = list(
            make_ingestor(db, bob, settings).ingest_files([("b.BYS", other_night)])
        )

        assert results[0].status == "failed"
        assert "another account" in results[0].reason
        assert db.query(Upload).filter_by(user_id=bob.id).count() == 0


class TestZipHandling:
    def _zip_of(self, entries: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        return buffer.getvalue()

    def test_imports_every_bys_in_an_archive(self, db, users, settings):
        alice, _ = users
        writer = FakeWriter()
        nights = {
            f"YUWELL/0010000{i}.BYS": build_bys(
                [build_record() for _ in range(20)],
                start=datetime(2025, 8, 20 + i, 22, 0, 0),
            )
            for i in range(1, 4)
        }
        archive = self._zip_of(nights)

        results = list(
            make_ingestor(db, alice, settings, writer).ingest_files([("card.zip", archive)])
        )

        assert len(results) == 3, f"expected 3 sessions from the archive, got {len(results)}"
        assert all(r.status == "imported" for r in results), [r.reason for r in results]

    def test_non_bys_entries_are_ignored_quietly(self, db, users, settings, sample_bys):
        alice, _ = users
        archive = self._zip_of(
            {
                "00100001.BYS": sample_bys,
                "System Volume Information/tracking.log": b"noise",
                "readme.txt": b"noise",
            }
        )

        results = list(make_ingestor(db, alice, settings).ingest_files([("card.zip", archive)]))

        assert len(results) == 1, "non-session files should not produce results"
        assert results[0].status == "imported"

    def test_rejects_path_traversal_entries(self, settings):
        archive = self._zip_of({"../../escape.BYS": b"x" * 62})

        with pytest.raises(ZipRejected, match="unsafe entry name"):
            list(extract_bys_from_zip(archive, settings))

    def test_rejects_absolute_path_entries(self, settings):
        archive = self._zip_of({"/etc/evil.BYS": b"x" * 62})

        with pytest.raises(ZipRejected, match="unsafe entry name"):
            list(extract_bys_from_zip(archive, settings))

    def test_rejects_too_many_entries(self, settings, tmp_path):
        small = Settings(
            database_url=f"sqlite:///{tmp_path / 'x.db'}", max_zip_entries=3
        )
        archive = self._zip_of({f"{i:08d}.BYS": b"x" * 10 for i in range(10)})

        with pytest.raises(ZipRejected, match="more than the limit"):
            list(extract_bys_from_zip(archive, small))

    def test_rejects_an_archive_that_expands_too_far(self, settings, tmp_path):
        """A zip bomb compresses to nothing and expands to gigabytes."""
        tiny = Settings(
            database_url=f"sqlite:///{tmp_path / 'x.db'}", max_zip_uncompressed_mb=1
        )
        archive = self._zip_of({"big.BYS": b"\x00" * (2 * 1024 * 1024)})

        with pytest.raises(ZipRejected, match="expands to"):
            list(extract_bys_from_zip(archive, tiny))

    def test_rejects_a_file_that_is_not_a_zip(self, db, users, settings):
        alice, _ = users
        results = list(
            make_ingestor(db, alice, settings).ingest_files([("card.zip", b"not a zip")])
        )

        assert results[0].status == "failed"
        assert "zip" in results[0].reason.lower()


class TestResumability:
    def test_reuploading_a_partially_imported_batch_completes_it(
        self, db, users, settings
    ):
        """The behaviour that replaces a resume button: everything already stored is
        skipped, and only what is missing gets written."""
        alice, _ = users
        writer = FakeWriter()

        nights = [
            (
                f"0010000{i}.BYS",
                build_bys(
                    [build_record() for _ in range(15)],
                    start=datetime(2025, 8, 10 + i, 22, 0, 0),
                ),
            )
            for i in range(1, 6)
        ]

        # First attempt stops after two files, as if the connection dropped.
        ingestor = make_ingestor(db, alice, settings, writer)
        stream = ingestor.ingest_files(nights)
        assert next(stream).status == "imported"
        assert next(stream).status == "imported"
        stream.close()

        assert db.query(Upload).count() == 2

        # The user simply uploads the whole folder again.
        results = list(make_ingestor(db, alice, settings, writer).ingest_files(nights))

        statuses = [r.status for r in results]
        assert statuses == ["duplicate", "duplicate", "imported", "imported", "imported"], (
            f"unexpected resume behaviour: {statuses}"
        )
        assert db.query(Upload).count() == 5
        assert len(writer.sessions) == 5, "already-stored sessions were written again"
