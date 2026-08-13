"""HTTP layer: login, upload with streamed progress, session list, health.

Charting lives in Grafana. These pages cover the things Grafana cannot do — getting
data in, and showing each account only its own sessions.
"""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    NotAuthenticated,
    authenticate,
    create_user,
    current_user,
    get_user_by_name,
    login_session,
    logout_session,
    user_count,
    verify_password,
)
from app.config import INSECURE_SECRET_KEYS, get_settings
from app.db import Upload, create_db_engine, create_session_factory, init_db
from app.influx import InfluxWriter
from app.ingest import Ingestor

# uvicorn configures only its own loggers and leaves the root logger at WARNING, so
# without this the first-run messages this app writes at INFO — including which
# account was just created — never reach the operator.
logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(levelname)s:     %(message)s",
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def device_localtime(value: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored timestamp in the machine's own timezone.

    Everything is stored in UTC, but the number a user wants to see is the one the
    machine displayed at the time. SQLite hands back naive datetimes and Postgres
    hands back aware ones, so normalise before converting.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(get_settings().device_zoneinfo).strftime(fmt)


templates.env.filters["devicetime"] = device_localtime
templates.env.globals["timedelta"] = timedelta

# Endpoints reachable without an account. Everything else requires current_user.
PUBLIC_PATHS = {"/login", "/setup", "/health", "/favicon.ico"}

MIN_PASSWORD_LENGTH = 8

# Rows per page on /sessions. The page was previously capped at 500 with no way to
# reach anything older, which quietly hid a third of one user's history.
PAGE_SIZE = 50


def clear_setup_token(app: FastAPI) -> None:
    """Close the setup window for good, in memory and on disk."""
    app.state.setup_token = None
    path = get_settings().setup_token_path
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - filesystem edge case
        log.warning("could not remove %s: %s", path, exc)


def _load_or_create_setup_token(settings) -> str:
    """The token that unlocks /setup, kept on disk between restarts.

    Requiring a value that only exists on the server means the person with access to
    the machine is the person who becomes admin. Without it, anyone who reaches the
    port while the instance has no accounts could claim it — and that window reopens
    if the database volume is ever lost while InfluxDB still holds the therapy data.

    It is stored rather than regenerated per boot so that finding it does not depend
    on catching the right lines in the log.
    """
    path = settings.setup_token_path
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - filesystem edge case
        log.warning("could not read %s: %s", path, exc)

    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n")
        path.chmod(0o600)
    except OSError as exc:  # pragma: no cover - filesystem edge case
        log.warning("could not write %s: %s — the token lives in memory only", path, exc)
    return token


def _warn_if_init_credentials_are_stale(db, settings) -> None:
    """INIT_* only ever creates the first account; it never updates one.

    Left unsaid, a stale value in .env reads as the current password and quietly
    misleads whoever comes back to the file months later.
    """
    if not settings.init_username:
        return
    user = get_user_by_name(db, settings.init_username.strip())
    if user is None:
        log.warning(
            "INIT_USERNAME is %r but no such account exists; INIT_* only applies while "
            "the instance has no accounts at all and has no effect now",
            settings.init_username,
        )
        return
    if settings.init_password and not verify_password(user.password_hash, settings.init_password):
        log.warning(
            "INIT_PASSWORD no longer matches the account %r. It was only used to create "
            "the account and has no effect now — change the password with: "
            "python -m app.cli passwd %s",
            user.username,
            user.username,
        )


def _bootstrap_access(app: FastAPI) -> None:
    """Decide how the first account gets created, once per boot.

    Three outcomes: nothing to do, seed an account from INIT_*, or open /setup behind
    a token. Only ever acts while the instance has no accounts.
    """
    settings = app.state.settings

    with app.state.db_factory() as db:
        if user_count(db) > 0:
            clear_setup_token(app)
            _warn_if_init_credentials_are_stale(db, settings)
            return

        username = settings.init_username.strip()
        password = settings.init_password

        if username and password:
            if len(password) < MIN_PASSWORD_LENGTH:
                # Refuse rather than silently standing up a weak administrator.
                log.error(
                    "INIT_PASSWORD is shorter than %d characters; no account was "
                    "created. Falling back to the setup token.",
                    MIN_PASSWORD_LENGTH,
                )
            else:
                create_user(db, username, password, is_admin=True)
                clear_setup_token(app)
                log.info(
                    "Created the first account %r from INIT_USERNAME/INIT_PASSWORD. "
                    "Those values have no effect from now on; use "
                    "'python -m app.cli passwd %s' to change the password.",
                    username,
                    username,
                )
                return
        elif username or password:
            log.error(
                "INIT_USERNAME and INIT_PASSWORD must both be set; ignoring the one "
                "that is. Falling back to the setup token."
            )

    token = _load_or_create_setup_token(settings)
    app.state.setup_token = token
    log.warning(
        "\n"
        "  ┌──────────────────────────────────────────────────────────────┐\n"
        "  │  No accounts exist yet. Open %-31s │\n"
        "  │  and enter this setup token to create the first account:     │\n"
        "  │                                                              │\n"
        "  │      %-56s│\n"
        "  │                                                              │\n"
        "  │  Also readable at %-43s│\n"
        "  │  Set INIT_USERNAME and INIT_PASSWORD in .env to skip this.   │\n"
        "  └──────────────────────────────────────────────────────────────┘",
        f"{settings.app_base_url}/setup",
        token,
        str(settings.setup_token_path),
    )


def _require_a_real_secret_key(settings) -> None:
    """Refuse to boot on a key anyone can read.

    SECRET_KEY signs the session cookie (see the SessionMiddleware setup below). A
    key that is empty, whitespace, or one of the values this repo has ever shipped as
    a default is public knowledge — anyone who has seen the source can forge
    `user_id=1` and read another account's therapy data. Warning and continuing
    anyway was rejected: a log line nobody reads doesn't stop that.
    """
    value = settings.secret_key
    if not value.strip() or value in INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "SECRET_KEY is unset or is a known placeholder value. Generate a real "
            "one and put it in .env: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _require_a_real_secret_key(settings)
    engine = create_db_engine(settings)
    init_db(engine)

    app.state.settings = settings
    app.state.engine = engine
    app.state.db_factory = create_session_factory(engine)
    app.state.influx = InfluxWriter(settings)
    _bootstrap_access(app)

    try:
        yield
    finally:
        app.state.influx.close()
        engine.dispose()


app = FastAPI(title="Yuwell PAP Logger", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.secret_key,
    https_only=_settings.cookies_secure,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    """One database session per request, closed on the way out."""
    factory = request.app.state.db_factory
    with factory() as db:
        request.state.db = db
        return await call_next(request)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, _exc: NotAuthenticated):
    """Send browsers to the login page; give API callers a 401."""
    if request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    ):
        return JSONResponse({"detail": "not signed in"}, status_code=401)
    nxt = request.url.path
    suffix = f"?next={nxt}" if nxt not in ("/", "/login") else ""
    return RedirectResponse(f"/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- auth


@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if user_count(request.state.db) > 0:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    setup_token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_repeat: str = Form(...),
):
    """Create the first account.

    Both gates are re-checked here rather than trusted from the GET: an account may
    have appeared in between, and the token is the only thing standing between a
    fresh instance and whoever else can reach the port.
    """
    db = request.state.db

    if user_count(db) > 0:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    expected = getattr(request.app.state, "setup_token", None)
    if not expected or not secrets.compare_digest(setup_token.strip(), expected):
        log.warning("rejected /setup attempt with an incorrect token")
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "setup token ไม่ถูกต้อง — ดูใน log ของคอนเทนเนอร์ app"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    username = username.strip()
    if not username:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "ต้องใส่ชื่อผู้ใช้"}, status_code=400
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร"},
            status_code=400,
        )
    if password != password_repeat:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "รหัสผ่านสองช่องไม่ตรงกัน"}, status_code=400
        )

    user = create_user(db, username, password, is_admin=True)
    # Burn the token: /setup is now closed for good on this instance.
    clear_setup_token(request.app)
    log.info("first account %r created via /setup; setup is now closed", username)

    login_session(request, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    # A fresh instance has nothing to sign in to; send people somewhere useful.
    if user_count(request.state.db) == 0:
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user = authenticate(request.state.db, username.strip(), password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    login_session(request, user)
    # Only ever redirect within this site.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# -------------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def upload_page(request: Request):
    user = current_user(request)
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "user": user,
            "grafana_url": settings.grafana_url,
            "max_files_per_request": settings.max_files_per_request,
            "max_upload_mb": settings.max_upload_mb,
        },
    )


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, page: int = 1):
    user = current_user(request)
    db = request.state.db

    total_rows = db.execute(
        select(func.count(Upload.id)).where(Upload.user_id == user.id)
    ).scalar_one()

    # Clamp rather than 404: a bookmarked page number goes stale as soon as new
    # sessions arrive and shift everything down.
    last_page = max(1, ceil(total_rows / PAGE_SIZE))
    page = max(1, min(page, last_page))
    offset = (page - 1) * PAGE_SIZE

    rows = (
        db.execute(
            select(Upload)
            .where(Upload.user_id == user.id)
            .order_by(Upload.session_start.desc())
            .offset(offset)
            .limit(PAGE_SIZE)
        )
        .scalars()
        .all()
    )

    # The machine writes a new file every time it is switched on, so one night is
    # routinely two to four sessions. Counting rows would report sessions while the
    # page says nights, and averaging per-session AHI would let a fifteen-minute
    # session at 5am weigh as much as a five-hour one.
    nights, session_count, total_minutes, ahi_minutes = db.execute(
        select(
            func.count(func.distinct(Upload.night_date)),
            func.count(Upload.id),
            func.sum(Upload.duration_minutes),
            func.sum(Upload.ahi * Upload.duration_minutes),
        ).where(Upload.user_id == user.id)
    ).one()

    nights = nights or 0
    total_minutes = total_minutes or 0

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "user": user,
            "grafana_url": request.app.state.settings.grafana_url,
            "device_tz": request.app.state.settings.device_tz,
            "uploads": rows,
            "page": page,
            "last_page": last_page,
            "page_first_row": offset + 1 if rows else 0,
            "page_last_row": offset + len(rows),
            "total_rows": total_rows,
            "total_nights": nights,
            "total_sessions": session_count or 0,
            "total_hours": total_minutes / 60,
            "hours_per_night": (total_minutes / nights / 60) if nights else 0.0,
            # sum(ahi_i * minutes_i) / sum(minutes_i) is exactly total events per hour
            # across the whole history, since ahi_i = events_i / minutes_i * 60.
            "average_ahi": ((ahi_minutes or 0) / total_minutes) if total_minutes else 0.0,
        },
    )


# ------------------------------------------------------------------------- upload


@app.post("/upload")
async def upload(request: Request, files: list[UploadFile]):
    """Import a batch of files, streaming one progress event per file.

    Server-sent events rather than a job queue: the browser gets real progress on an
    800-file import, the request keeps producing output so proxies do not time it
    out, and there is no worker process or job table to operate. Resumability comes
    from the per-file ledger commits in Ingestor, not from job state.
    """
    user = current_user(request)
    settings = request.app.state.settings
    db = request.state.db

    if len(files) > settings.max_files_per_request:
        return JSONResponse(
            {
                "detail": (
                    f"{len(files)} files in one request, limit is "
                    f"{settings.max_files_per_request}. Upload in smaller batches, or "
                    f"zip the folder and upload that."
                )
            },
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    payloads: list[tuple[str, bytes]] = []
    total_bytes = 0
    for upload_file in files:
        data = await upload_file.read()
        total_bytes += len(data)
        if total_bytes > settings.max_upload_bytes:
            return JSONResponse(
                {
                    "detail": (
                        f"upload exceeds {settings.max_upload_mb} MB. Upload in smaller "
                        f"batches."
                    )
                },
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        payloads.append((upload_file.filename or "unnamed", data))

    ingestor = Ingestor(
        db=db, user=user, settings=settings, writer=request.app.state.influx
    )

    def event_stream():
        counts = {"imported": 0, "duplicate": 0, "failed": 0}
        done = 0
        try:
            for result in ingestor.ingest_files(payloads):
                done += 1
                counts[result.status] = counts.get(result.status, 0) + 1
                yield _sse(
                    "file",
                    {
                        "done": done,
                        "filename": result.filename,
                        "status": result.status,
                        "reason": result.reason,
                        "night_date": result.night_date,
                        "minutes": result.minutes,
                        "ahi": round(result.ahi, 1),
                        "warnings": result.warnings or [],
                    },
                )
            yield _sse("done", {"counts": counts, "total": done})
        except Exception as exc:  # noqa: BLE001 - surface anything to the browser
            log.exception("upload failed")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------------------- health


@app.get("/health")
async def health(request: Request):
    """Reports which InfluxDB this instance is actually talking to.

    Public on purpose so it can be used as a container healthcheck; it exposes no
    therapy data and no credentials.
    """
    influx = request.app.state.influx.health()
    healthy = influx["reachable"] and influx["bucket_found"]
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "influx": influx},
        status_code=200 if healthy else 503,
    )
