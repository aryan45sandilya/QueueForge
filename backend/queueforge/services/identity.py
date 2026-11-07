"""Account lifecycle: email verification, password reset and change, account deletion.

Tokens are random, single-use, stored as SHA-256 hashes and expire. Endpoints that take an
email address (forgot password, resend verification) behave identically whether or not the
account exists, and the emails are sent after the response, so neither content nor timing
reveals which addresses are registered.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.errors import AppError, Unauthorized, ValidationFailed
from queueforge.models import EmailToken, Project, ProjectMember, Session, User
from queueforge.security import hash_password, hash_session_token, verify_password
from queueforge.services.common import Actor, audit, db_now
from queueforge.services.email import OutgoingEmail, reset_email, verification_email

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)


class InvalidToken(AppError):
    status_code = 400
    code = "invalid_token"


async def _issue_token(session: AsyncSession, user: User, purpose: str, ttl: timedelta) -> str:
    """Create a token and invalidate any earlier unused ones for the same purpose."""
    now = await db_now(session)
    await session.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user.id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_session_token(token),
            expires_at=now + ttl,
        )
    )
    return token


async def _find_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    user: User | None = result.scalar_one_or_none()
    return user


async def _consume(session: AsyncSession, token: str, purpose: str) -> User:
    now = await db_now(session)
    row = (
        await session.execute(
            select(EmailToken, User)
            .join(User, User.id == EmailToken.user_id)
            .where(
                EmailToken.token_hash == hash_session_token(token), EmailToken.purpose == purpose
            )
            .with_for_update(of=EmailToken)
        )
    ).one_or_none()
    if (
        row is None
        or row[0].used_at is not None
        or row[0].expires_at <= now
        or not row[1].is_active
    ):
        raise InvalidToken("This link is invalid or has expired. Request a new one.")
    row[0].used_at = now
    user: User = row[1]
    return user


# --- verification ---------------------------------------------------------------------------


async def verification_for_new_user(db: Database, settings: Settings, user: User) -> OutgoingEmail:
    async with db.transaction() as session:
        token = await _issue_token(session, user, "verify_email", VERIFY_TTL)
    return verification_email(settings, user.email, user.display_name, token)


async def resend_verification(db: Database, settings: Settings, email: str) -> OutgoingEmail | None:
    async with db.transaction() as session:
        user = await _find_user_by_email(session, email)
        if user is None or not user.is_active or user.email_verified_at is not None:
            return None
        token = await _issue_token(session, user, "verify_email", VERIFY_TTL)
    return verification_email(settings, user.email, user.display_name, token)


async def verify_email(db: Database, token: str) -> User:
    async with db.transaction() as session:
        user = await _consume(session, token, "verify_email")
        if user.email_verified_at is None:
            user.email_verified_at = await db_now(session)
        audit(
            session,
            project_id=None,
            actor=Actor(label=f"user:{user.email}", user_id=user.id),
            action="auth.email_verified",
            target_type="user",
            target_id=str(user.id),
        )
    return user


# --- passwords ------------------------------------------------------------------------------


async def request_password_reset(
    db: Database, settings: Settings, email: str
) -> OutgoingEmail | None:
    async with db.transaction() as session:
        user = await _find_user_by_email(session, email)
        if user is None or not user.is_active:
            return None
        token = await _issue_token(session, user, "reset_password", RESET_TTL)
    return reset_email(settings, user.email, user.display_name, token)


async def reset_password(db: Database, token: str, new_password: str) -> User:
    async with db.transaction() as session:
        user = await _consume(session, token, "reset_password")
        user.password_hash = hash_password(new_password)
        # Following the emailed link proves the address belongs to them.
        if user.email_verified_at is None:
            user.email_verified_at = await db_now(session)
        # Sign out everywhere: whoever knew the old password loses access.
        await session.execute(delete(Session).where(Session.user_id == user.id))
        audit(
            session,
            project_id=None,
            actor=Actor(label=f"user:{user.email}", user_id=user.id),
            action="auth.password_reset",
            target_type="user",
            target_id=str(user.id),
        )
    return user


async def change_password(
    db: Database, user_id: uuid.UUID, current_session_id: uuid.UUID, current: str, new: str
) -> None:
    async with db.transaction() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one()
        if not verify_password(user.password_hash, current):
            raise Unauthorized("Current password is incorrect", code="wrong_password")
        if current == new:
            raise ValidationFailed(
                "The new password must be different from the current one",
                details=[
                    {
                        "loc": ["new_password"],
                        "msg": "same as current password",
                        "type": "value_error",
                    }
                ],
            )
        user.password_hash = hash_password(new)
        # Keep this session, end all others.
        await session.execute(
            delete(Session).where(Session.user_id == user.id, Session.id != current_session_id)
        )
        audit(
            session,
            project_id=None,
            actor=Actor(label=f"user:{user.email}", user_id=user.id),
            action="auth.password_changed",
            target_type="user",
            target_id=str(user.id),
        )


# --- account deletion -----------------------------------------------------------------------


async def delete_account(db: Database, user_id: uuid.UUID, password: str) -> int:
    """Delete the user and every project where they are the only owner. Returns projects deleted.

    Projects with another owner are kept; the user just leaves them. Jobs, attempts, schedules,
    queues and keys of deleted projects are removed by cascade. Redis dispatch keys of deleted
    queues are dropped by the reconciler.
    """
    async with db.transaction() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one()
        if not verify_password(user.password_hash, password):
            raise Unauthorized("Password is incorrect", code="wrong_password")
        owned = (
            (
                await session.execute(
                    select(ProjectMember.project_id).where(
                        ProjectMember.user_id == user_id, ProjectMember.role == "owner"
                    )
                )
            )
            .scalars()
            .all()
        )
        sole: list[uuid.UUID] = []
        for project_id in owned:
            other_owners = (
                await session.execute(
                    select(func.count())
                    .select_from(ProjectMember)
                    .where(
                        ProjectMember.project_id == project_id,
                        ProjectMember.role == "owner",
                        ProjectMember.user_id != user_id,
                    )
                )
            ).scalar_one()
            if other_owners == 0:
                sole.append(project_id)
        if sole:
            await session.execute(delete(Project).where(Project.id.in_(sole)))
        await session.delete(user)
    return len(sole)
