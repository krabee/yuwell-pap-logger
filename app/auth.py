"""Accounts and the session-cookie login.

Deliberately small: one household, a handful of accounts, no self-registration, no
OAuth, no JWT. Accounts are created with `python -m app.cli adduser`.

Passwords use Argon2. A fast hash (md5, sha*) would let anyone who reaches the
database file try the whole password list in seconds.
"""

from __future__ import annotations

from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.db import User

_hasher = PasswordHasher()

SESSION_USER_KEY = "user_id"


class NotAuthenticated(HTTPException):
    """Raised for browser navigation, turned into a redirect to /login."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses older Argon2 parameters than the current ones."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def get_user_by_name(db: DBSession, username: str) -> User | None:
    return db.execute(select(User).where(User.username == username)).scalar_one_or_none()


def user_count(db: DBSession) -> int:
    """How many accounts exist. Drives the first-run setup gate, so it is always read
    live rather than cached — an account may appear from the CLI at any moment."""
    return db.execute(select(func.count()).select_from(User)).scalar_one()


def create_user(db: DBSession, username: str, password: str, is_admin: bool = False) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    return user


def authenticate(db: DBSession, username: str, password: str) -> User | None:
    user = get_user_by_name(db, username)
    if user is None:
        # Spend the same time as a real verification so that a wrong username and a
        # wrong password are not distinguishable by response time.
        _hasher.hash(password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    return user


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id


def logout_session(request: Request) -> None:
    request.session.clear()


def _db_from_request(request: Request) -> DBSession:
    """The request-scoped database session opened by the middleware in main.py."""
    return request.state.db


def current_user(request: Request) -> User:
    """Dependency for every route that touches a person's data.

    There is no 'optional user' variant on purpose: a route either requires an
    account or is one of the handful of public endpoints listed in main.py.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        raise NotAuthenticated()

    db = _db_from_request(request)
    user = db.get(User, user_id)
    if user is None:
        # Account deleted while the cookie was still valid.
        request.session.clear()
        raise NotAuthenticated()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def redirect_to_login(request: Request) -> RedirectResponse:
    target = request.url.path
    suffix = f"?next={target}" if target not in ("/", "/login") else ""
    return RedirectResponse(url=f"/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)
