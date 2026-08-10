"""Turning uploaded bytes into stored sessions.

Written as a generator that yields one result per file, for three reasons:

  * the web layer can stream progress to the browser without a job queue,
  * the CLI can use exactly the same code path,
  * each file is committed as it finishes, so an upload that dies halfway leaves
    everything it already did in place. Re-uploading the whole batch then picks up
    where it stopped, because finished files are recognised and skipped.

That last point matters: a first import is 700-800 files off an SD card.
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import timezone
from typing import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.config import Settings
from app.db import Device, Upload, User
from app.influx import InfluxUnavailable, InfluxWriter, session_id
from app.metrics import summarise
from app.parsers.base import InvalidPAPFile
from app.parsers.registry import parse as parse_session

log = logging.getLogger(__name__)

STATUS_IMPORTED = "imported"
STATUS_DUPLICATE = "duplicate"
STATUS_FAILED = "failed"

_BYS_SUFFIX = ".bys"


@dataclass(slots=True)
class IngestResult:
    """What happened to one file. `reason` is shown to the user verbatim."""

    filename: str
    status: str
    reason: str = ""
    session_id: str = ""
    night_date: str = ""
    minutes: int = 0
    ahi: float = 0.0
    points_written: int = 0
    warnings: list[str] | None = None


class ZipRejected(Exception):
    """The archive is malformed or looks hostile."""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_bys_name(name: str) -> bool:
    return name.lower().endswith(_BYS_SUFFIX)


def extract_bys_from_zip(data: bytes, settings: Settings) -> Iterator[tuple[str, bytes]]:
    """Yield (name, bytes) for every .bys entry in an uploaded archive.

    Uploaded archives are untrusted. Three things are checked before anything is read:
    entry names that would escape the extraction root, an entry count that suggests a
    generated archive, and a total uncompressed size far beyond what an SD card holds.
    Nothing is ever written to disk, so path traversal cannot overwrite files here —
    the name check is still applied because a traversing name has no legitimate reason
    to appear and is a good signal to stop.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ZipRejected(f"not a readable zip archive: {exc}") from exc

    entries = archive.infolist()
    if len(entries) > settings.max_zip_entries:
        raise ZipRejected(
            f"archive holds {len(entries)} entries, more than the limit of "
            f"{settings.max_zip_entries}"
        )

    total_uncompressed = sum(e.file_size for e in entries)
    if total_uncompressed > settings.max_zip_uncompressed_bytes:
        raise ZipRejected(
            f"archive expands to {total_uncompressed / 1024 / 1024:.1f} MB, more than "
            f"the limit of {settings.max_zip_uncompressed_mb} MB"
        )

    for entry in entries:
        if entry.is_dir():
            continue
        name = entry.filename
        if name.startswith("/") or name.startswith("\\") or ".." in name.replace("\\", "/").split("/"):
            raise ZipRejected(f"archive contains an unsafe entry name: {name!r}")
        if not is_bys_name(name):
            continue  # SD cards carry other files; ignore them quietly
        yield name.rsplit("/", 1)[-1], archive.read(entry)


class Ingestor:
    """Imports files for one account.

    Holds the database session and one Influx client for the whole batch rather than
    reconnecting per file.
    """

    def __init__(
        self,
        db: DBSession,
        user: User,
        settings: Settings,
        writer: InfluxWriter | None = None,
    ):
        self._db = db
        self._user = user
        self._settings = settings
        self._writer = writer if writer is not None else InfluxWriter(settings)
        self._owns_writer = writer is None

    def close(self) -> None:
        if self._owns_writer:
            self._writer.close()

    def already_imported(self, file_hash: str) -> Upload | None:
        return self._db.execute(
            select(Upload).where(
                Upload.user_id == self._user.id, Upload.file_sha256 == file_hash
            )
        ).scalar_one_or_none()

    def _check_device_ownership(self, serial: str, model: str) -> str | None:
        """Claim the machine for this account, or refuse if someone else owns it.

        Returns an error message, or None when the upload may proceed. Silently
        merging two people's therapy data would be much worse than a clear refusal.
        """
        device = self._db.execute(
            select(Device).where(Device.serial == serial)
        ).scalar_one_or_none()

        if device is None:
            self._db.add(Device(serial=serial, user_id=self._user.id, model=model))
            self._db.commit()
            return None

        if device.user_id != self._user.id:
            return (
                f"machine {serial} is already registered to another account on this "
                f"server; sign in as that account to import its data"
            )
        return None

    def ingest_one(self, filename: str, data: bytes) -> IngestResult:
        file_hash = sha256_of(data)

        # Hash first, parse second. Re-uploading the same SD card is the normal case,
        # and this makes almost all of it free.
        existing = self.already_imported(file_hash)
        if existing is not None:
            return IngestResult(
                filename=filename,
                status=STATUS_DUPLICATE,
                reason=f"already imported on {existing.imported_at:%Y-%m-%d}",
                session_id=existing.session_id,
                night_date=existing.night_date,
                minutes=existing.duration_minutes,
                ahi=existing.ahi,
            )

        try:
            session = parse_session(data)
        except InvalidPAPFile as exc:
            return IngestResult(filename=filename, status=STATUS_FAILED, reason=str(exc))

        ownership_error = self._check_device_ownership(session.serial, session.model)
        if ownership_error is not None:
            return IngestResult(
                filename=filename, status=STATUS_FAILED, reason=ownership_error
            )

        summary = summarise(session)
        sid = session_id(session.serial, session.start)

        try:
            points = self._writer.write_session(
                session=session,
                summary=summary,
                username=self._user.username,
                source_file=filename,
                file_sha256=file_hash,
            )
        except InfluxUnavailable as exc:
            # No ledger row: the next attempt must retry this file rather than
            # treating it as done.
            return IngestResult(filename=filename, status=STATUS_FAILED, reason=str(exc))

        start_utc = session.start.replace(
            tzinfo=self._settings.device_zoneinfo
        ).astimezone(timezone.utc)

        self._db.add(
            Upload(
                user_id=self._user.id,
                file_sha256=file_hash,
                filename=filename,
                serial=session.serial,
                model=session.model,
                session_id=sid,
                session_start=start_utc,
                night_date=summary.night.isoformat(),
                duration_minutes=summary.duration_minutes,
                ahi=summary.ahi,
                avg_pressure=summary.avg_pressure,
                p90_pressure=summary.p90_pressure,
                avg_leak=summary.avg_leak,
                points_written=points,
                warnings="; ".join(session.warnings)[:2000],
            )
        )
        # Commit per file: this is what makes an interrupted bulk upload resumable.
        self._db.commit()

        return IngestResult(
            filename=filename,
            status=STATUS_IMPORTED,
            session_id=sid,
            night_date=summary.night.isoformat(),
            minutes=summary.duration_minutes,
            ahi=summary.ahi,
            points_written=points,
            warnings=session.warnings or None,
        )

    def ingest_files(self, files: Iterable[tuple[str, bytes]]) -> Iterator[IngestResult]:
        """Import a stream of (filename, bytes), expanding any zip archives.

        Yields as it goes so callers can report progress; files are never all held in
        memory at once by this function.
        """
        for filename, data in files:
            if filename.lower().endswith(".zip"):
                try:
                    for inner_name, inner_data in extract_bys_from_zip(
                        data, self._settings
                    ):
                        yield self.ingest_one(inner_name, inner_data)
                except ZipRejected as exc:
                    yield IngestResult(
                        filename=filename, status=STATUS_FAILED, reason=str(exc)
                    )
                continue

            if not is_bys_name(filename):
                yield IngestResult(
                    filename=filename,
                    status=STATUS_FAILED,
                    reason="not a .bys session file",
                )
                continue

            yield self.ingest_one(filename, data)
