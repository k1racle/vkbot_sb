"""Durable reminders keep accepting contacts; no real VK, secrets or sleeps."""

import asyncio
import copy
import json
import weakref
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from test_scenarios import setup as setup, event, create
from test_waits import timer as timer, install, tick
from test_flow_modules import graph
from test_project_storage import storage as storage
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    callback,
    message,
    session_for,
)
from app import clients, db, dialog, projects, vk_api, waits
from app.flows import advance, validate_graph


def contact_graph(**options):
    return graph(
        {
            "id": "phone",
            "type": "contact",
            "text": "Оставьте телефон",
            "variable": "phone",
            "allow_skip": False,
            "reminder_enabled": True,
            "reminder_text": "Напоминание, {first_name}",
            "allow_later": True,
            "later_text": "Хорошо, {first_name}",
            "later_reminder_enabled": True,
            "later_reminder_text": "Напоминание после Позже",
            "next": "end",
            **options,
        },
        {"id": "end", "type": "end", "text": "Контакт получен: {phone}"},
    )


def start(timer, **options):
    client, _, sent, _, _ = timer
    flow = install(client, contact_graph(**options))
    assert client.post("/vk/callback", json=event()).status_code == 200
    return flow, json.loads(
        sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"]
    )


@pytest.mark.parametrize("prefix", ["reminder", "later_reminder"])
@pytest.mark.parametrize(
    "value,unit",
    [(0, "hours"), (True, "days"), (1.5, "hours"), ("3", "hours"), (1, "months")],
)
def test_duration_schema(prefix, value, unit):
    with pytest.raises(ValidationError):
        contact_graph(**{prefix + "_delay_value": value, prefix + "_delay_unit": unit})


def test_limits_defaults_and_legacy_published_graph(timer):
    assert validate_graph(
        contact_graph(reminder_delay_value=3651, reminder_delay_unit="days")
    )
    assert validate_graph(
        contact_graph(later_reminder_delay_value=3651, later_reminder_delay_unit="days")
    )
    assert not validate_graph(
        contact_graph(reminder_delay_value=3650, reminder_delay_unit="days")
    )
    client, sessions, sent, current, _ = timer
    data = contact_graph()
    for key in list(data["nodes"][1]):
        if key.startswith(("reminder_", "later_")) or key == "allow_later":
            data["nodes"][1].pop(key)
    flow = install(client, data)
    with sessions() as session:
        session.get(db.Scenario, flow["id"]).published = data
        session.commit()
    client.post("/vk/callback", json=event())
    assert sent.call_args.kwargs["keyboard"]["buttons"] == []
    with sessions() as session:
        assert session.query(db.ScenarioWait).count() == 0
    current[0] += timedelta(days=1)
    tick()
    client.post("/vk/callback", json=event("89991234567", 2))
    assert sent.call_args.args[1] == "Контакт получен: +79991234567"


@pytest.mark.parametrize("when", ["before", "due", "after_reminder"])
def test_valid_phone_cancels_and_can_arrive_after_reminder(timer, when):
    client, sessions, sent, current, _ = timer
    start(timer)
    if when != "before":
        current[0] += timedelta(hours=3)
    if when == "after_reminder":
        db.init_db()  # Simulate startup; pending timer and contact mode survive.
        tick()
        assert sent.call_args.args[1] == "Напоминание, Анна"
        with sessions() as session:
            assert session.get(db.Conversation, 77).node_id == "phone"
    client.post("/vk/callback", json=event("8 (999) 123-45-67", 2))
    client.post("/vk/callback", json=event("8 (999) 123-45-67", 2))
    assert sent.call_args.args[1] == "Контакт получен: +79991234567"
    count = sent.await_count
    current[0] += timedelta(days=2)
    tick()
    tick()
    assert sent.await_count == count
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and "_contact_reminder" not in row.variables
        assert "_wait_id" not in row.variables
        assert session.query(db.ScenarioWait).one().status == (
            "done" if when == "after_reminder" else "cancelled"
        )
        assert session.get(db.Client, 77).phone == "+79991234567"


def test_invalid_number_does_not_move_deadline_and_reminder_only_once(timer):
    client, sessions, sent, current, _ = timer
    start(timer, error_text="Проверьте номер")
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        ident, due = job.id, job.due_at
    current[0] += timedelta(hours=1)
    for number, text in enumerate(
        ["123", "не знаю", "", "89991234567 telegram a@b.ru"], 2
    ):
        client.post("/vk/callback", json=event(text, number))
        assert sent.call_args.args[1] == "Проверьте номер"
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert (job.id, job.due_at, job.status) == (ident, due, "pending")
        assert not session.get(db.Client, 77).phone
    current[0] = due
    tick()
    assert sent.call_args.args[1] == "Напоминание, Анна"
    count = sent.await_count
    current[0] += timedelta(days=2)
    tick()
    assert sent.await_count == count


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("button", [False, True])
def test_later_replaces_timer_and_keeps_collecting(timer, enabled, button):
    client, sessions, sent, current, _ = timer
    _, payload = start(
        timer,
        later_reminder_enabled=enabled,
        later_reminder_delay_value=5,
        later_reminder_delay_unit="minutes",
    )
    current[0] += timedelta(hours=1)
    client.post(
        "/vk/callback", json=event("Позже", 2, payload=payload if button else None)
    )
    client.post(
        "/vk/callback", json=event("Позже", 2, payload=payload if button else None)
    )
    assert sent.call_args.args[1] == "Хорошо, Анна" and sent.await_count == 2
    with sessions() as session:
        jobs = session.query(db.ScenarioWait).all()
        assert len(jobs) == (2 if enabled else 1)
        assert sum(j.status == "cancelled" for j in jobs) == 1
        assert session.get(db.Conversation, 77).node_id == "phone"
        if enabled:
            assert next(j for j in jobs if j.status == "pending").due_at == current[
                0
            ] + timedelta(minutes=5)
    current[0] += timedelta(minutes=5)
    tick()
    assert sent.call_args.args[1] == (
        "Напоминание после Позже" if enabled else "Хорошо, Анна"
    )
    client.post("/vk/callback", json=event("89991234567", 3))
    assert sent.call_args.args[1] == "Контакт получен: +79991234567"


def test_repeated_later_reschedules_once_per_distinct_message(timer):
    client, sessions, _, current, _ = timer
    start(timer)
    for number in (2, 3):
        current[0] += timedelta(hours=1)
        client.post("/vk/callback", json=event("Позже", number))
    with sessions() as session:
        assert session.query(db.ScenarioWait).count() == 3
        assert session.query(db.ScenarioWait).filter_by(
            status="pending"
        ).one().due_at == current[0] + timedelta(hours=24)


@pytest.mark.parametrize("change", ["nonce", "flow", "node", "contact_action"])
def test_stale_later_button_does_not_cancel_reminder(timer, change):
    client, sessions, sent, _, _ = timer
    _, payload = start(timer)
    payload[change] = "old"
    client.post("/vk/callback", json=event("Позже", 2, payload=payload))
    assert "устарела" in sent.call_args.args[1]
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "pending"


def test_skip_cancels_and_is_distinct_from_later(timer):
    client, sessions, sent, _, _ = timer
    start(timer, allow_skip=True)
    client.post("/vk/callback", json=event("Пропустить", 2))
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == ""
        assert session.query(db.ScenarioWait).one().status == "cancelled"
        assert not session.get(db.Client, 77).phone
    assert "Контакт получен:" in sent.call_args.args[1]  # Next block chosen by admin.


@pytest.mark.parametrize(
    "action",
    [
        "stop",
        "operator",
        "pause",
        "publish",
        "delete",
        "chat_off",
        "cancel",
        "permission",
        "deny",
    ],
)
def test_cancellation_paths(timer, action):
    client, sessions, sent, current, allowed = timer
    flow, _ = start(timer)
    assert client.get("/admin/api/conversations").json()[0]["wait"]["contact_reminder"]
    if action in {"stop", "operator"}:
        client.post(
            "/vk/callback", json=event("Стоп" if action == "stop" else "менеджер", 2)
        )
    elif action in {"pause", "publish", "delete"}:
        assert (
            client.post(
                f"/admin/api/scenarios/{flow['id']}/{action}",
                json={"revision": flow["revision"]},
            ).status_code
            == 200
        )
    elif action == "cancel":
        assert client.post("/admin/api/conversations/77/cancel-wait").status_code == 200
    elif action == "permission":
        allowed.return_value = False
    elif action == "deny":
        client.post(
            "/vk/callback",
            json={
                "type": "message_deny",
                "group_id": 123,
                "secret": "test-secret",
                "object": {"user_id": 77},
            },
        )
    else:
        with sessions() as session:
            db.save_settings(session, {"chat_enabled": "false"})
    count = sent.await_count
    current[0] += timedelta(days=2)
    tick()
    assert sent.await_count == count
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"
    if action == "cancel":
        client.post("/vk/callback", json=event("89991234567", 3))
        assert sent.call_args.args[1] == "Контакт получен: +79991234567"


def test_retry_uses_stable_send_id_and_can_be_cancelled_by_number(timer):
    client, sessions, sent, current, _ = timer
    start(timer)
    current[0] += timedelta(hours=3)
    sent.side_effect = httpx.ConnectError("temporary")
    tick()
    ident = sent.call_args.kwargs["random_id"]
    current[0] += timedelta(seconds=30)
    tick()
    assert sent.call_args.kwargs["random_id"] == ident
    sent.side_effect = None
    client.post("/vk/callback", json=event("89991234567", 2))
    count = sent.await_count
    current[0] += timedelta(hours=1)
    tick()
    assert sent.await_count == count
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"


def test_failed_reminder_still_accepts_late_contact(timer):
    client, sessions, sent, current, _ = timer
    start(timer)
    current[0] += timedelta(hours=3)
    sent.side_effect = ValueError("do not persist secrets")
    tick()
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "failed"
        assert session.get(db.Conversation, 77).node_id == "phone"
    sent.side_effect = None
    client.post("/vk/callback", json=event("89991234567", 2))
    assert sent.call_args.args[1] == "Контакт получен: +79991234567"


def test_preview_later_validation_and_reminder_are_virtual(timer):
    client, sessions, sent, _, allowed = timer
    create(client)
    data = contact_graph()
    state = {}

    def preview(**kwargs):
        nonlocal state
        response = client.post(
            "/admin/api/preview", json={"graph": data, "state": state, **kwargs}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        state = result["state"]
        return result["messages"]

    first = preview()
    deadline = state["waiting"]["deadline"]
    assert "Введите телефон" in preview(text="123")[0]["text"]
    assert state["waiting"]["deadline"] == deadline
    payload = json.loads(first[0]["keyboard"]["buttons"][0][0]["action"]["payload"])
    assert preview(payload=payload)[0]["text"] == "Хорошо, Анна"
    assert state["waiting"]["seconds"] == 86400
    assert preview(resume_wait=True)[0]["text"] == "Напоминание после Позже"
    assert state["node_id"] == "phone" and "waiting" not in state
    assert preview(text="89991234567")[0]["text"] == "Контакт получен: +79991234567"
    assert state["phone_status"] == "provided"
    with sessions() as session:
        assert (
            session.query(db.Client).count() == 0
            and session.query(db.ScenarioWait).count() == 0
        )
    sent.assert_not_called()
    allowed.assert_not_called()


def test_email_has_same_reminder_flow_without_saving_as_phone(timer):
    client, sessions, sent, current, _ = timer
    start(
        timer, contact_type="email", variable="email", reminder_text="Напомнить email"
    )
    current[0] += timedelta(hours=3)
    tick()
    assert sent.call_args.args[1] == "Напомнить email"
    client.post("/vk/callback", json=event("name@example.com", 2))
    with sessions() as session:
        assert session.get(db.Conversation, 77).variables["email"] == "name@example.com"
        assert not session.get(db.Client, 77).phone


def test_phone_then_email_in_subflow_replaces_timer_and_returns(timer):
    client, sessions, sent, current, _ = timer
    phone = contact_graph()["nodes"][1]
    phone["next"] = "email"
    data = graph(
        {
            "id": "call",
            "type": "call_subflow",
            "subflow_id": "contacts",
            "return_variables": ["email"],
            "next": "end",
        },
        {"id": "end", "type": "end", "text": "Спасибо: {email}"},
        {"id": "contacts", "type": "subflow", "next": "phone"},
        phone,
        {
            **phone,
            "id": "email",
            "text": "Теперь email",
            "contact_type": "email",
            "variable": "email",
            "reminder_text": "Не забудьте email",
            "next": "back",
        },
        {"id": "back", "type": "return"},
    )
    install(client, data)
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("89991234567", 2))
    with sessions() as session:
        assert (
            session.query(db.ScenarioWait).filter_by(status="cancelled").one().node_id
            == "phone"
        )
        assert (
            session.query(db.ScenarioWait).filter_by(status="pending").one().node_id
            == "email"
        )
        assert session.get(db.Client, 77).phone == "+79991234567"
    current[0] += timedelta(hours=3)
    tick()
    assert sent.call_args.args[1] == "Не забудьте email"
    client.post("/vk/callback", json=event("name@example.com", 3))
    assert sent.call_args.args[1] == "Спасибо: name@example.com"
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and not row.variables.get("_stack")
        assert "phone" not in row.variables and "_contact_reminder" not in row.variables


def test_reminder_isolated_between_projects(two_projects, monkeypatch):
    env, first, second = two_projects
    for project in (first, second):
        route = prefix(project) + "/api/scenarios"
        flow = env.client.post(route).json()
        flow = env.client.put(
            f"{route}/{flow['id']}",
            json={
                "title": "Contact",
                "graph": contact_graph(
                    reminder_delay_value=1, reminder_delay_unit="seconds"
                ),
                "revision": flow["revision"],
            },
        ).json()
        assert (
            env.client.post(
                f"{route}/{flow['id']}/publish", json={"revision": flow["revision"]}
            ).status_code
            == 200
        )
        with session_for(project) as session:
            db.save_settings(session, {"chat_enabled": "true"})
        callback(env.client, project, message(project))
    # A phone in the first group must cancel only its reminder.
    callback(env.client, first, message(first, "89991234567", 2))
    env.network.calls.clear()
    now = clients.now() + timedelta(hours=1)
    monkeypatch.setattr(clients, "now", lambda: now)

    async def run():
        for project in (first, second):
            with projects.project_scope(project):
                await waits.worker_tick()

    asyncio.run(run())
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert len(sends) == 1 and sends[0]["project_id"] == second.id


def test_real_storage_timer_and_contact_transition(storage, monkeypatch):
    monkeypatch.setattr(dialog, "_locks", weakref.WeakValueDictionary())
    monkeypatch.setattr(dialog, "CALLBACK_SLOTS", asyncio.Semaphore(4))
    (project,) = projects.init_registry()
    sent = AsyncMock()
    monkeypatch.setattr(vk_api, "send_message", sent)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    data = contact_graph()
    with projects.project_scope(project), db.SessionLocal() as session:
        db.save_settings(session, {"chat_enabled": "true"})
        session.add(
            db.Scenario(
                id=1,
                title="Contact",
                published=data,
                draft=data,
                active=True,
                version=1,
            )
        )
        session.add(db.Client(user_id=77, messages_allowed=True))
        session.add(
            db.Conversation(
                user_id=77,
                scenario_id=1,
                version=1,
                node_id="phone",
                variables={
                    "_wait_id": "contact-job",
                    "_nonce": "visit",
                    "first_name": "Анна",
                    "_contact_reminder": {"node_id": "phone", "mode": "silence"},
                },
            )
        )
        session.add(
            db.ScenarioWait(
                id="contact-job",
                user_id=77,
                active_user=77,
                scenario_id=1,
                version=1,
                node_id="phone",
                due_at=clients.now() - timedelta(seconds=1),
            )
        )
        session.commit()
        db.init_db()

    async def run():
        with projects.project_scope(project):
            if storage.engine.dialect.name == "postgresql":
                with db.SessionLocal() as guard:
                    dialog.lock_conversation(guard, 77)
                    await waits.worker_tick()
                    sent.assert_not_called()
            await asyncio.gather(waits.worker_tick(), waits.worker_tick())
            with db.SessionLocal() as session:
                row = session.get(db.Conversation, 77)
                assert row.node_id == "phone"
                state = {
                    "node_id": row.node_id,
                    "version": 1,
                    "variables": copy.deepcopy(row.variables),
                }
                port = dialog.LivePort(session, 77, "late-number", {})
                await advance(data, state, "89991234567", {}, port)
                assert state["node_id"] == ""
                session.commit()

    asyncio.run(run())
    assert [c.args[1] for c in sent.call_args_list] == [
        "Напоминание, Анна",
        "Контакт получен: +79991234567",
    ]
