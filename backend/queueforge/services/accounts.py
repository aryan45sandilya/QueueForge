"""Users, sessions, projects, memberships, API keys and queues."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, DispatchUnavailable
from queueforge.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationFailed
from queueforge.lifecycle import JobState
from queueforge.models import ApiKey, Job, Project, ProjectMember, Queue, Session, User
from queueforge.schemas import (
    ApiKeyCreate,
    ApiKeyOut,
    MembershipOut,
    ProjectCreate,
    ProjectOut,
    ProjectSettingsUpdate,
    QueueCreate,
    QueueOut,
    QueueUpdate,
)
from queueforge.security import (
    generate_api_key,
    hash_api_secret,
    hash_password,
    hash_session_token,
    new_csrf_token,
    new_session_token,
    parse_api_key,
    password_needs_rehash,
    verify_api_secret,
    verify_password,
)
from queueforge.services.common import Actor, audit, db_now

# --- users and sessions ---------------------------------------------------------------------


async def register_user(db: Database, email: str, password: str, display_name: str) -> User:
    async with db.transaction() as session:
        user = User(
            id=uuid.uuid4(),
            email=email.strip(),
            display_name=display_name.strip(),
            password_hash=hash_password(password),
        )
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError as exc:
            raise Conflict("An account with this email already exists") from exc
    return user


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    expires_at: datetime
    user: User


async def login(
    db: Database,
    settings: Settings,
    email: str,
    password: str,
    user_agent: str | None,
    ip: str | None,
) -> NewSession:
    async with db.transaction() as session:
        user = (
            await session.execute(
                select(User).where(func.lower(User.email) == email.strip().lower())
            )
        ).scalar_one_or_none()
        # verify_password runs a full hash even for unknown users, so timing doesn't leak existence.
        if (
            not verify_password(user.password_hash if user else None, password)
            or user is None
            or not user.is_active
        ):
            raise Unauthorized("Invalid email or password")
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        now = await db_now(session)
        token = new_session_token()
        csrf = new_csrf_token()
        expires = now + timedelta(hours=settings.session_ttl_hours)
        session.add(
            Session(
                user_id=user.id,
                token_hash=hash_session_token(token),
                csrf_token=csrf,
                expires_at=expires,
                last_seen_at=now,
                user_agent=(user_agent or "")[:255] or None,
                ip_address=ip,
            )
        )
        user.last_login_at = now
        audit(
            session,
            project_id=None,
            actor=Actor(label=f"user:{user.email}", user_id=user.id),
            action="auth.login",
            target_type="user",
            target_id=str(user.id),
        )
    return NewSession(token=token, csrf_token=csrf, expires_at=expires, user=user)


async def resolve_session(db: Database, token: str) -> tuple[User, Session] | None:
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(User, Session)
                .join(Session, Session.user_id == User.id)
                .where(
                    Session.token_hash == hash_session_token(token),
                    Session.expires_at > func.now(),
                    User.is_active.is_(True),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        user, sess = row
        now = await db_now(session)
        if (now - sess.last_seen_at) > timedelta(minutes=5):
            sess.last_seen_at = now
    return user, sess


async def logout(db: Database, token: str) -> None:
    async with db.transaction() as session:
        sess = (
            await session.execute(
                select(Session).where(Session.token_hash == hash_session_token(token))
            )
        ).scalar_one_or_none()
        if sess is not None:
            await session.delete(sess)


async def memberships(db: Database, user_id: uuid.UUID) -> list[MembershipOut]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Project, ProjectMember.role)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(ProjectMember.user_id == user_id)
                .order_by(Project.name)
            )
        ).all()
    return [MembershipOut(project_id=p.id, slug=p.slug, name=p.name, role=r) for p, r in rows]


# --- projects -------------------------------------------------------------------------------


def project_out(project: Project, role: str | None) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.role = role
    return out


async def create_project(db: Database, user: User, req: ProjectCreate) -> ProjectOut:
    async with db.transaction() as session:
        project = Project(id=uuid.uuid4(), slug=req.slug, name=req.name)
        try:
            async with session.begin_nested():
                session.add(project)
                await session.flush()
        except IntegrityError as exc:
            raise Conflict(f"Project slug {req.slug!r} is already taken") from exc
        session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
        session.add(Queue(project_id=project.id, name="default", description="Default queue"))
        audit(
            session,
            project_id=project.id,
            actor=Actor(label=f"user:{user.email}", user_id=user.id),
            action="project.created",
            target_type="project",
            target_id=str(project.id),
        )
        await session.flush()
        await session.refresh(project)
    return project_out(project, "owner")


async def update_project(
    db: Database, project_id: uuid.UUID, req: ProjectSettingsUpdate, actor: Actor
) -> ProjectOut:
    changes = req.model_dump(exclude_unset=True, exclude_none=True)
    async with db.transaction() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id).with_for_update())
        ).scalar_one()
        before = {k: getattr(project, k) for k in changes}
        for key, value in changes.items():
            setattr(project, key, value)
        if project.default_backoff_base_seconds > project.default_backoff_max_seconds:
            raise ValidationFailed(
                "default_backoff_base_seconds must not exceed default_backoff_max_seconds"
            )
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="project.settings_updated",
            target_type="project",
            target_id=str(project_id),
            details={"changes": {k: {"from": before[k], "to": v} for k, v in changes.items()}},
        )
    return project_out(project, "owner")


async def get_membership(
    db: Database, project_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Project, str] | None:
    async with db.session() as session:
        row = (
            await session.execute(
                select(Project, ProjectMember.role)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(Project.id == project_id, ProjectMember.user_id == user_id)
            )
        ).one_or_none()
    return (row[0], row[1]) if row else None


# --- API keys -------------------------------------------------------------------------------


def key_status(key: ApiKey, now: datetime) -> str:
    if key.revoked_at is not None:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= now:
        return "expired"
    if key.expires_at is not None:
        return "expiring"
    return "active"


def key_out(key: ApiKey, now: datetime) -> ApiKeyOut:
    out = ApiKeyOut.model_validate(key)
    out.status = key_status(key, now)
    return out


async def list_keys(db: Database, project_id: uuid.UUID) -> list[ApiKeyOut]:
    async with db.session() as session:
        now = await db_now(session)
        keys = (
            (
                await session.execute(
                    select(ApiKey)
                    .where(ApiKey.project_id == project_id)
                    .order_by(ApiKey.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [key_out(k, now) for k in keys]


async def create_key(
    db: Database,
    settings: Settings,
    project_id: uuid.UUID,
    req: ApiKeyCreate,
    actor: Actor,
    rotated_from: uuid.UUID | None = None,
) -> tuple[ApiKeyOut, str]:
    generated = generate_api_key()
    async with db.transaction() as session:
        now = await db_now(session)
        key = ApiKey(
            id=uuid.uuid4(),
            project_id=project_id,
            name=req.name,
            prefix=generated.prefix,
            secret_hash=hash_api_secret(settings.secret_key, generated.secret),
            scopes=req.scopes,
            created_by_user_id=actor.user_id,
            rotated_from_id=rotated_from,
            created_at=now,
            expires_at=now + timedelta(days=req.expires_in_days) if req.expires_in_days else None,
        )
        session.add(key)
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="api_key.created",
            target_type="api_key",
            target_id=str(key.id),
            details={"name": req.name, "prefix": key.prefix, "scopes": req.scopes},
        )
    return key_out(key, now), generated.full


async def _load_key(session: object, project_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
    key = (
        await session.execute(  # type: ignore[attr-defined]
            select(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if key is None:
        raise NotFound("API key not found")
    return key


async def revoke_key(
    db: Database, project_id: uuid.UUID, key_id: uuid.UUID, actor: Actor
) -> ApiKeyOut:
    async with db.transaction() as session:
        key = await _load_key(session, project_id, key_id)
        now = await db_now(session)
        if key.revoked_at is None:
            key.revoked_at = now
            audit(
                session,
                project_id=project_id,
                actor=actor,
                action="api_key.revoked",
                target_type="api_key",
                target_id=str(key.id),
                details={"prefix": key.prefix},
            )
    return key_out(key, now)


async def rotate_key(
    db: Database,
    settings: Settings,
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    grace_hours: int,
    actor: Actor,
) -> tuple[ApiKeyOut, str]:
    async with db.transaction() as session:
        old = await _load_key(session, project_id, key_id)
        now = await db_now(session)
        if key_status(old, now) in ("revoked", "expired"):
            raise Conflict("Only active keys can be rotated")
        if grace_hours == 0:
            old.revoked_at = now
        else:
            new_expiry = now + timedelta(hours=grace_hours)
            old.expires_at = min(old.expires_at, new_expiry) if old.expires_at else new_expiry
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="api_key.rotated",
            target_type="api_key",
            target_id=str(old.id),
            details={"prefix": old.prefix, "grace_hours": grace_hours},
        )
        name, scopes = old.name, list(old.scopes)
    return await create_key(
        db, settings, project_id, ApiKeyCreate(name=name, scopes=scopes), actor, rotated_from=key_id
    )


async def authenticate_key(db: Database, settings: Settings, raw: str) -> tuple[ApiKey, Project]:
    parsed = parse_api_key(raw)
    if parsed is None:
        raise Unauthorized("Malformed API key")
    prefix, secret = parsed
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(ApiKey, Project)
                .join(Project, Project.id == ApiKey.project_id)
                .where(ApiKey.prefix == prefix)
            )
        ).one_or_none()
        if row is None or not verify_api_secret(settings.secret_key, secret, row[0].secret_hash):
            raise Unauthorized("Invalid API key")
        key, project = row
        now = await db_now(session)
        if key.revoked_at is not None:
            raise Unauthorized("API key has been revoked")
        if key.expires_at is not None and key.expires_at <= now:
            raise Unauthorized("API key has expired")
        if key.last_used_at is None or (now - key.last_used_at) > timedelta(minutes=1):
            await session.execute(
                update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now)
            )
    return key, project


# --- queues ---------------------------------------------------------------------------------


async def list_queues(
    db: Database, dispatcher: Dispatcher | None, project_id: uuid.UUID
) -> list[QueueOut]:
    async with db.session() as session:
        queues = (
            (
                await session.execute(
                    select(Queue).where(Queue.project_id == project_id).order_by(Queue.name)
                )
            )
            .scalars()
            .all()
        )
        counts = (
            await session.execute(
                select(Job.queue_id, Job.state, func.count(), func.min(Job.queued_at))
                .where(
                    Job.project_id == project_id,
                    Job.state.in_(
                        [JobState.QUEUED, JobState.RUNNING, JobState.SCHEDULED, JobState.RETRYING]
                    ),
                )
                .group_by(Job.queue_id, Job.state)
            )
        ).all()
    depth: dict[str, int] = {}
    if dispatcher is not None:
        try:
            depth = await dispatcher.depth([q.id for q in queues])
        except DispatchUnavailable:
            depth = {}
    out: list[QueueOut] = []
    for q in queues:
        item = QueueOut.model_validate(q)
        for queue_id, state, count, oldest in counts:
            if queue_id == q.id:
                setattr(item, state, int(count))
                if state == JobState.QUEUED:
                    item.oldest_queued_at = oldest
        item.dispatch_depth = depth.get(str(q.id))
        out.append(item)
    return out


async def create_queue(
    db: Database, project_id: uuid.UUID, req: QueueCreate, actor: Actor
) -> QueueOut:
    async with db.transaction() as session:
        queue = Queue(
            id=uuid.uuid4(),
            project_id=project_id,
            name=req.name,
            description=req.description,
            concurrency_limit=req.concurrency_limit,
            paused=False,
        )
        try:
            async with session.begin_nested():
                session.add(queue)
                await session.flush()
        except IntegrityError as exc:
            raise Conflict(f"Queue {req.name!r} already exists") from exc
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="queue.created",
            target_type="queue",
            target_id=str(queue.id),
            details=req.model_dump(),
        )
        await session.refresh(queue)
    return QueueOut.model_validate(queue)


async def update_queue(
    db: Database, project_id: uuid.UUID, queue_id: uuid.UUID, req: QueueUpdate, actor: Actor
) -> QueueOut:
    async with db.transaction() as session:
        queue = (
            await session.execute(
                select(Queue)
                .where(Queue.id == queue_id, Queue.project_id == project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if queue is None:
            raise NotFound("Queue not found")
        changes = req.model_dump(exclude_unset=True)
        if req.description is not None:
            queue.description = req.description
        if req.clear_concurrency_limit:
            queue.concurrency_limit = None
        elif req.concurrency_limit is not None:
            queue.concurrency_limit = req.concurrency_limit
        if req.paused is not None:
            queue.paused = req.paused
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="queue.updated",
            target_type="queue",
            target_id=str(queue.id),
            details={"name": queue.name, "changes": changes},
        )
    return QueueOut.model_validate(queue)


async def delete_queue(
    db: Database,
    dispatcher: Dispatcher | None,
    project_id: uuid.UUID,
    queue_id: uuid.UUID,
    actor: Actor,
) -> None:
    async with db.transaction() as session:
        queue = (
            await session.execute(
                select(Queue)
                .where(Queue.id == queue_id, Queue.project_id == project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if queue is None:
            raise NotFound("Queue not found")
        if queue.name == "default":
            raise Forbidden("The default queue cannot be deleted")
        jobs = (
            await session.execute(
                select(func.count()).select_from(Job).where(Job.queue_id == queue_id)
            )
        ).scalar_one()
        if jobs:
            raise Conflict(
                f"Queue still has {jobs} job record(s). Pause it instead, or wait for retention to remove them.",
                details={"jobs": int(jobs)},
            )
        from queueforge.models import Schedule

        schedules = (
            await session.execute(
                select(func.count()).select_from(Schedule).where(Schedule.queue_id == queue_id)
            )
        ).scalar_one()
        if schedules:
            raise Conflict(
                "Queue is used by schedules; delete or move them first",
                details={"schedules": int(schedules)},
            )
        await session.delete(queue)
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="queue.deleted",
            target_type="queue",
            target_id=str(queue_id),
            details={"name": queue.name},
        )
    if dispatcher is not None:
        # If Redis is down the reconciler drops keys of deleted queues later.
        with contextlib.suppress(DispatchUnavailable):
            await dispatcher.delete_queue(queue_id)
