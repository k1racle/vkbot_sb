"""Durable, project-local scenario timers. No sleeping requests or external queue."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import update

from . import clients, db, projects, vk_api
from .config import get_settings
from .flows import advance, contact_reminder

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4


def cancel_waits(session, reason, *, user_id=None, scenario_id=None):
    query = session.query(db.ScenarioWait).filter_by(status="pending")
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    if scenario_id is not None:
        query = query.filter_by(scenario_id=scenario_id)
    return query.update({"status": "cancelled", "active_user": None, "error": reason})


def schedule_wait(session, conversation, state):
    waiting = state.get("waiting")
    if not waiting:
        return
    cancel_waits(session, "Начато новое ожидание", user_id=conversation.user_id)
    ident = uuid4().hex
    due = clients.now() + timedelta(seconds=waiting["seconds"])
    session.add(
        db.ScenarioWait(
            id=ident,
            user_id=conversation.user_id,
            active_user=conversation.user_id,
            scenario_id=conversation.scenario_id,
            version=conversation.version,
            node_id=waiting["node_id"],
            due_at=due,
            expires_at=due if waiting.get("kind") == "wait_reply" else None,
            created_at=clients.now(),
        )
    )
    conversation.variables = dict(conversation.variables, _wait_id=ident)


def event_for(session, job):
    key = f"wait:{job.id}"
    event = session.query(db.DialogEvent).filter_by(event_key=key).first()
    if event is None:
        event = db.DialogEvent(
            event_key=key,
            user_id=job.user_id,
            kind="wait",
            text="Продолжение сценария после ожидания",
        )
        session.add(event)
    return event


def clear_wait_reference(row):
    # A contact reminder is not a deadline for accepting the number. Even if
    # sending failed or an admin cancelled it, keep the contact question open.
    pending_contact = row.variables.get("_contact_reminder", {})
    if pending_contact.get("node_id") != row.node_id:
        row.node_id = ""
    row.variables = {
        k: v
        for k, v in row.variables.items()
        if k not in {"_wait_id", "_contact_reminder"}
    }


def stop(session, job, reason, *, status="cancelled"):
    job.status, job.active_user, job.error = status, None, reason
    row = session.get(db.Conversation, job.user_id)
    if row and row.variables.get("_wait_id") == job.id:
        clear_wait_reference(row)
    event = event_for(session, job)
    event.status, event.error = status, reason
    session.commit()


async def worker_tick():
    """Process at most one due job. Retry with stable VK random_ids after crashes."""
    from .dialog import CALLBACK_SLOTS, LivePort, lock_conversation, truth, user_lock

    project = projects.current_project.get()
    if project is not None:
        fresh = projects.get_project(project.id)
        if fresh is None or not fresh.enabled:
            return
    with db.SessionLocal() as session:
        candidate = (
            session.query(db.ScenarioWait.id, db.ScenarioWait.user_id)
            .filter(
                db.ScenarioWait.status == "pending",
                db.ScenarioWait.due_at <= clients.now(),
            )
            .order_by(db.ScenarioWait.due_at, db.ScenarioWait.id)
            .first()
        )
    if candidate is None:
        return
    ident, user_id = candidate
    async with user_lock(user_id), CALLBACK_SLOTS:
        with clients.defer_outgoing_audits(), db.SessionLocal() as session:
            if not lock_conversation(session, user_id, wait=False):
                return
            # Claim inside the transaction; a crash rolls back to pending. The
            # conditional write also serializes SQLite workers without a lease.
            claimed = session.execute(
                update(db.ScenarioWait)
                .where(
                    db.ScenarioWait.id == ident,
                    db.ScenarioWait.status == "pending",
                    db.ScenarioWait.due_at <= clients.now(),
                )
                .values(status="running")
            ).rowcount
            if not claimed:
                return
            job = session.get(db.ScenarioWait, ident)
            try:
                row = session.get(db.Conversation, user_id)
                scenario = session.get(db.Scenario, job.scenario_id)
                values = db.read_settings(session)
                client = session.get(db.Client, user_id)
                if (
                    not row
                    or row.handoff
                    or row.scenario_id != job.scenario_id
                    or row.version != job.version
                    or row.node_id != job.node_id
                    or row.variables.get("_wait_id") != job.id
                ):
                    stop(session, job, "Диалог уже изменился или передан менеджеру")
                    return
                if (
                    not scenario
                    or not scenario.active
                    or scenario.is_deleted
                    or scenario.version != job.version
                    or not scenario.published
                ):
                    stop(
                        session,
                        job,
                        "Сценарий выключен, удалён или опубликована новая версия",
                    )
                    return
                if not truth(values.get("chat_enabled") or get_settings().chat_enabled):
                    stop(session, job, "Общение с ботом выключено")
                    return
                waiting_node = next(
                    (
                        node
                        for node in scenario.published.get("nodes", [])
                        if node.get("id") == job.node_id
                    ),
                    None,
                )
                if not waiting_node or waiting_node.get("type") not in {
                    "wait",
                    "wait_reply",
                    "contact",
                }:
                    stop(
                        session,
                        job,
                        "Блок ожидания отсутствует в опубликованном сценарии",
                    )
                    return
                if waiting_node["type"] == "contact":
                    pending_contact = row.variables.get("_contact_reminder", {})
                    mode = pending_contact.get("mode")
                    if (
                        pending_contact.get("node_id") != job.node_id
                        or mode not in {"silence", "later"}
                        or not contact_reminder(waiting_node, mode)[0]
                    ):
                        stop(session, job, "Напоминание о контакте больше не требуется")
                        return
                if (
                    not client
                    or client.unsubscribed
                    or client.deactivated
                    or client.messages_allowed is False
                ):
                    stop(session, job, "Клиент отписался или сообщения недоступны")
                    return
                if not await vk_api.is_messages_allowed(user_id):
                    client.messages_allowed = False
                    stop(session, job, "Нет разрешения на сообщения сообщества")
                    return
                event = event_for(session, job)
                variables = dict(row.variables)
                variables.pop("_wait_id", None)
                state = {
                    "node_id": row.node_id,
                    "version": row.version,
                    "variables": variables,
                    "handoff": False,
                    "nonce": variables.pop("_nonce", ""),
                    "stack": variables.pop("_stack", []),
                }
                row.variables = variables
                port = LivePort(session, user_id, event.event_key, values)
                # Free the unique active slot before a following wait is scheduled.
                job.status, job.active_user = "done", None
                session.flush()
                await advance(
                    scenario.published,
                    state,
                    variables.get("last_message", ""),
                    {},
                    port,
                    resume_wait=True,
                )
                row.node_id, row.handoff = state["node_id"], state.get("handoff", False)
                row.variables = dict(
                    state["variables"],
                    _nonce=state.get("nonce", ""),
                    _stack=state.get("stack", []),
                )
                schedule_wait(session, row, state)
                event.status, event.error, job.error = "done", port.warning, ""
                session.commit()
            except asyncio.CancelledError:
                session.rollback()
                raise
            except Exception as error:
                session.rollback()
                # Reacquire after rollback: a STOP/restart may have won the race.
                if not lock_conversation(session, user_id, wait=False):
                    return
                job = session.get(db.ScenarioWait, ident)
                if job is None or job.status != "pending":
                    return
                job.attempts += 1
                denied = isinstance(error, vk_api.VkApiError) and error.code in {
                    901,
                    902,
                }
                retryable = isinstance(error, httpx.HTTPError) or (
                    isinstance(error, vk_api.VkApiError)
                    and error.code in {1, 6, 9, 10, 29}
                )
                # Do not persist arbitrary exception text (it can contain credentials).
                reason = (
                    f"VK: ошибка {error.code}"
                    if isinstance(error, vk_api.VkApiError)
                    else "Не удалось продолжить сценарий после ожидания"
                )
                if denied or not retryable or job.attempts >= MAX_ATTEMPTS:
                    stop(
                        session, job, reason, status="cancelled" if denied else "failed"
                    )
                else:
                    job.due_at = clients.now() + timedelta(
                        seconds=30 * 2 ** (job.attempts - 1)
                    )
                    job.error = reason
                    event = event_for(session, job)
                    event.status, event.error = "retry", reason
                    session.commit()
                logger.warning("Scenario wait %s: %s", ident, reason)
