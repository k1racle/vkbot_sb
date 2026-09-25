"""Durable mailings. Only explicit admin confirmation enqueues real sends."""

import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, or_, update

from . import db, vk_api
from .clients import insert_once, now, save_profile
from .config import get_settings
from .flows import render
from .gifts import random_id
from .operators import configured_operators

logger = logging.getLogger(__name__)
FOOTER = "\n\nЧтобы отказаться от рассылок, напишите «Стоп»."
LEASE_SECONDS = 300
MAX_ATTEMPTS = 4


def keyboard():
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "Отписаться от рассылок",
                        "payload": json.dumps({"action": "broadcast_unsubscribe"}),
                    }
                }
            ]
        ],
    }


def content(template, client):
    return (
        render(
            template,
            {
                "first_name": client.first_name or "друг",
                "user_name": client.first_name or "друг",
                "last_name": client.last_name,
            },
        )
        + FOOTER
    )


def counts(session, broadcast_id):
    return dict(
        session.query(
            db.BroadcastRecipient.status, func.count(db.BroadcastRecipient.id)
        )
        .filter_by(broadcast_id=broadcast_id)
        .group_by(db.BroadcastRecipient.status)
        .all()
    )


def serialize(session, item):
    tally = counts(session, item.id)
    asset = session.get(db.MediaAsset, item.media_id) if item.media_id else None
    return {
        "id": item.id,
        "title": item.title,
        "message": item.message,
        "media_id": item.media_id,
        "filename": asset.filename if asset else "",
        "status": item.status,
        "error": item.error,
        "counts": tally,
        "total": sum(tally.values()),
        "created_at": str(item.created_at),
    }


def claim_worker(owner):
    with db.SessionLocal() as session:
        insert_once(session, db.WorkLease, {"name": "outbound_jobs"}, ["name"])
        changed = session.execute(
            update(db.WorkLease)
            .where(
                db.WorkLease.name == "outbound_jobs",
                or_(db.WorkLease.until.is_(None), db.WorkLease.until < now()),
            )
            .values(owner=owner, until=now() + timedelta(seconds=LEASE_SECONDS))
        ).rowcount
        session.commit()
        return changed == 1


def release_worker(owner):
    with db.SessionLocal() as session:
        session.execute(
            update(db.WorkLease)
            .where(
                db.WorkLease.name == "outbound_jobs",
                db.WorkLease.owner == owner,
            )
            .values(until=None)
        )
        session.commit()


def claim_recipient():
    with db.SessionLocal() as session:
        # Paused/cancelled jobs can retain an interrupted send after a crash.
        # Wait for its lease before releasing it; never race an in-flight request.
        for row, job_status in (
            session.query(db.BroadcastRecipient, db.Broadcast.status)
            .join(db.Broadcast, db.Broadcast.id == db.BroadcastRecipient.broadcast_id)
            .filter(
                db.BroadcastRecipient.status == "sending",
                db.BroadcastRecipient.lease_until < now(),
                db.Broadcast.status.in_(["paused", "cancelled"]),
            )
        ):
            row.status = "cancelled" if job_status == "cancelled" else "pending"
            row.lease_until = None
        session.commit()
        # Finish jobs only when no pending or in-flight recipient remains.
        for job in (
            session.query(db.Broadcast)
            .filter(db.Broadcast.status.in_(["queued", "running"]))
            .order_by(db.Broadcast.created_at)
        ):
            if not job.consent_confirmed:
                job.status, job.error = "paused", "Не подтверждено согласие получателей"
                session.commit()
                continue
            row = (
                session.query(db.BroadcastRecipient)
                .filter(
                    db.BroadcastRecipient.broadcast_id == job.id,
                    or_(
                        (db.BroadcastRecipient.status == "pending")
                        & or_(
                            db.BroadcastRecipient.next_attempt_at.is_(None),
                            db.BroadcastRecipient.next_attempt_at <= now(),
                        ),
                        (db.BroadcastRecipient.status == "sending")
                        & (db.BroadcastRecipient.lease_until < now()),
                    ),
                )
                .order_by(db.BroadcastRecipient.id)
                .first()
            )
            if row:
                token = uuid4().hex
                row.status, row.lease_token = "sending", token
                row.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
                row.attempts += 1
                job.status = "running"
                session.commit()
                return row.id, token
            if (
                not session.query(db.BroadcastRecipient)
                .filter(
                    db.BroadcastRecipient.broadcast_id == job.id,
                    db.BroadcastRecipient.status.in_(["pending", "sending"]),
                )
                .first()
            ):
                job.status, job.finished_at = "completed", now()
                session.commit()
    return None


def still_eligible(row_id, token):
    with db.SessionLocal() as session:
        row = session.get(db.BroadcastRecipient, row_id)
        if not row or row.status != "sending" or row.lease_token != token:
            return False
        job, client = (
            session.get(db.Broadcast, row.broadcast_id),
            session.get(db.Client, row.user_id),
        )
        if job.status not in {"queued", "running"}:
            row.status = "cancelled" if job.status == "cancelled" else "pending"
            row.lease_until = None
            session.commit()
            return False
        operators = configured_operators(db.read_settings(session), get_settings())
        if (
            not client
            or not client.bot_contacted_at
            or client.unsubscribed
            or client.deactivated
            or row.user_id in operators
        ):
            row.status, row.error, row.lease_until = (
                "skipped",
                "Получатель исключён или отписался",
                None,
            )
            session.commit()
            return False
        return True


def finish_recipient(row_id, token, status, error=""):
    with db.SessionLocal() as session:
        row = session.get(db.BroadcastRecipient, row_id)
        if row and row.lease_token == token:
            row.status, row.error, row.lease_until = status, error[:1000], None
            if status == "sent":
                row.sent_at = now()
            session.commit()


async def send_recipient(row_id, token):
    if not still_eligible(row_id, token):
        return
    with db.SessionLocal() as session:
        row = session.get(db.BroadcastRecipient, row_id)
        job = session.get(db.Broadcast, row.broadcast_id)
        user_id, job_id, attempts = row.user_id, job.id, row.attempts
        payload = dict(row.payload)
        client = session.get(db.Client, user_id)
        template = content(job.message, client)
        asset = session.get(db.MediaAsset, job.media_id) if job.media_id else None
    try:
        if (
            job.media_id
            and not payload
            and (not asset or not Path(asset.path).is_file())
        ):
            finish_recipient(
                row_id,
                token,
                "failed",
                "Файл вложения недоступен. Создайте рассылку с новым файлом",
            )
            return
        allowed = await vk_api.is_messages_allowed(user_id)
        with db.SessionLocal() as session:
            session.get(db.Client, user_id).messages_allowed = allowed
            session.commit()
        if not allowed:
            finish_recipient(
                row_id,
                token,
                "skipped",
                "VK: получатель не разрешил сообщения сообщества",
            )
            return
        if not payload:
            attachment = ""
            if asset:
                attachment = await vk_api.upload_file_for_message(
                    user_id, Path(asset.path), asset.filename, asset.content_type
                )
            payload = {"text": template, "attachment": attachment}
            # Store immutable content BEFORE the send. Retries and restarts reuse
            # it and the same VK random_id, not a freshly rendered message.
            with db.SessionLocal() as session:
                row = session.get(db.BroadcastRecipient, row_id)
                if row.lease_token != token:
                    return
                row.payload = payload
                session.commit()
        if not still_eligible(row_id, token):
            return
        await vk_api.send_message(
            user_id,
            payload["text"],
            random_id=random_id(f"broadcast:{job_id}:{user_id}"),
            attachment=payload["attachment"],
            keyboard=keyboard(),
        )
        finish_recipient(row_id, token, "sent")
    except (vk_api.VkApiError, httpx.HTTPError, OSError) as error:
        with db.SessionLocal() as session:
            current = session.get(db.BroadcastRecipient, row_id)
            if (
                not current
                or current.lease_token != token
                or current.status != "sending"
            ):
                return
            cancelled = session.get(db.Broadcast, job_id).status == "cancelled"
        if cancelled:
            finish_recipient(row_id, token, "cancelled", str(error))
            return
        code = getattr(error, "code", None)
        if code in {901, 902, 18}:
            finish_recipient(row_id, token, "skipped", str(error))
            with db.SessionLocal() as session:
                session.get(db.Client, user_id).messages_allowed = False
                session.commit()
        elif code in {5, 7, 9, 14, 15, 27}:
            # Invalid token/access is a job problem, not thousands of failed users.
            with db.SessionLocal() as session:
                job = session.get(db.Broadcast, job_id)
                if job.status != "cancelled":
                    job.status, job.error = (
                        "paused",
                        f"VK остановил отправку. Проверьте права, ограничения и токен: {error}"[
                            :1000
                        ],
                    )
                row = session.get(db.BroadcastRecipient, row_id)
                row.status, row.lease_until = "pending", None
                row.error = str(error)[:1000]
                session.commit()
        elif attempts < MAX_ATTEMPTS and (
            isinstance(error, httpx.HTTPError) or code in {1, 6, 10}
        ):
            with db.SessionLocal() as session:
                row = session.get(db.BroadcastRecipient, row_id)
                row.status, row.lease_until = "pending", None
                row.next_attempt_at = now() + timedelta(
                    seconds=min(300, 5 * 2**attempts)
                )
                row.error = str(error)[:1000]
                session.commit()
        else:
            finish_recipient(row_id, token, "failed", str(error))


async def refresh_profiles():
    with db.SessionLocal() as session:
        ids = [
            row.user_id
            for row in session.query(db.Client)
            .filter_by(profile_requested=True)
            .order_by(db.Client.user_id)
            .limit(50)
        ]
    if not ids:
        return False
    try:
        profiles = await vk_api.call(
            "users.get", user_ids=",".join(map(str, ids)), fields="photo_100,contacts"
        )
        with db.SessionLocal() as session:
            returned = set()
            for profile in profiles:
                if isinstance(profile, dict) and profile.get("id") in ids:
                    returned.add(profile["id"])
                    save_profile(session, profile)
            for user_id in set(ids) - returned:
                client = session.get(db.Client, user_id)
                client.profile_requested, client.profile_error = (
                    False,
                    "VK не вернул профиль",
                )
            session.commit()
    except (vk_api.VkApiError, httpx.HTTPError) as error:
        with db.SessionLocal() as session:
            session.query(db.Client).filter(db.Client.user_id.in_(ids)).update(
                {
                    "profile_requested": False,
                    "profile_error": f"Не удалось обновить профиль: {error}"[:1000],
                },
                synchronize_session=False,
            )
            session.commit()
    return True


async def worker_tick():
    owner = uuid4().hex
    if not claim_worker(owner):
        return
    try:
        recipient = claim_recipient()
        if recipient:
            await send_recipient(*recipient)
        else:
            await refresh_profiles()
    finally:
        release_worker(owner)


async def worker_loop():
    while True:
        try:
            await worker_tick()
        except Exception:
            logger.exception("Background outbound job failed")
        # Conservative pace; VK rate-limit responses use persisted backoff too.
        await asyncio.sleep(1.0)


async def stop_worker(task):
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
