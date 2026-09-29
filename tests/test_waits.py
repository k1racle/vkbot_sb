"""Timers use an isolated database and mocked VK; no real sleeps or sends."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from test_scenarios import setup as setup, create, event, publish
from test_flow_modules import graph
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    callback,
    message,
    session_for,
)
from test_project_storage import storage as storage
from app import clients, db, vk_api, waits
from app.flows import Graph, validate_graph, wait_seconds


def wait_graph(value=3, unit="hours"):
    return graph(
        {"id": "hello", "type": "message", "text": "До паузы", "next": "wait"},
        {
            "id": "wait",
            "type": "wait",
            "delay_value": value,
            "delay_unit": unit,
            "next": "after",
        },
        {
            "id": "after",
            "type": "message",
            "text": "После паузы, {first_name}",
            "next": "end",
        },
        {"id": "end", "type": "end"},
    )


def install(client, data=None):
    flow = create(client)
    result = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={
            "title": "Ожидание",
            "revision": flow["revision"],
            "graph": data or wait_graph(),
        },
    )
    assert result.status_code == 200, result.text
    return publish(client, result.json())


@pytest.fixture
def timer(setup, monkeypatch):
    client, sessions, sent = setup
    current = [datetime(2026, 9, 29, 10)]
    monkeypatch.setattr(clients, "now", lambda: current[0])
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(vk_api, "is_messages_allowed", allowed)
    return client, sessions, sent, current, allowed


def tick():
    asyncio.run(waits.worker_tick())


@pytest.mark.parametrize(
    "value,unit,seconds",
    [
        (1, "seconds", 1),
        (90, "minutes", 5400),
        (3, "hours", 10800),
        (3650, "days", 315360000),
    ],
)
def test_duration_validation(value, unit, seconds):
    data = wait_graph(value, unit)
    assert wait_seconds(data["nodes"][2]) == seconds
    assert not validate_graph(data)


@pytest.mark.parametrize(
    "value,unit",
    [
        (0, "hours"),
        (-1, "days"),
        (1.5, "hours"),
        (True, "seconds"),
        ("3", "hours"),
        (1, "months"),
    ],
)
def test_bad_duration_rejected(value, unit):
    with pytest.raises(ValidationError):
        wait_graph(value, unit)


def test_limit_and_wait_does_not_allow_automatic_loops():
    assert validate_graph(wait_graph(3651, "days"))
    data = wait_graph()
    data["nodes"][2]["next"] = "hello"
    assert any("цикл" in issue for issue in validate_graph(data))


def test_preview_manual_clock_never_uses_vk_or_persists_jobs(timer):
    client, sessions, sent, _, allowed = timer
    create(client)
    data = wait_graph()
    first = client.post("/admin/api/preview", json={"graph": data}).json()
    assert first["state"]["waiting"]["seconds"] == 10800
    assert [m["text"] for m in first["messages"]] == ["До паузы"]
    blocked = client.post(
        "/admin/api/preview",
        json={"graph": data, "state": first["state"], "text": "Ответ"},
    ).json()
    assert blocked["messages"] == []
    resumed = client.post(
        "/admin/api/preview",
        json={"graph": data, "state": blocked["state"], "resume_wait": True},
    ).json()
    assert [m["text"] for m in resumed["messages"]] == ["После паузы, Анна"]
    assert "waiting" not in resumed["state"]
    with sessions() as session:
        assert session.query(db.ScenarioWait).count() == 0
    sent.assert_not_called()
    allowed.assert_not_called()


def test_survives_reinitialization_and_fires_once_at_due_time(timer):
    client, sessions, sent, current, allowed = timer
    install(client)
    assert client.post("/vk/callback", json=event()).status_code == 200
    client.post("/vk/callback", json=event())
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert job.due_at == current[0] + timedelta(hours=3)
        assert job.status == "pending"
        ident = job.id
    db.init_db()  # Startup's additive migration keeps both timers and dialogs.
    current[0] += timedelta(hours=3, seconds=-1)
    tick()
    assert sent.await_count == 1
    current[0] += timedelta(seconds=1)
    tick()
    tick()
    assert sent.await_count == 2
    assert sent.call_args.args[1] == "После паузы, Анна"
    allowed.assert_awaited_once_with(77)
    with sessions() as session:
        assert session.get(db.ScenarioWait, ident).status == "done"
        assert session.get(db.Conversation, 77).node_id == ""
        assert (
            session.query(db.DialogEvent).filter_by(kind="wait", status="done").count()
            == 1
        )


def test_incoming_does_not_shorten_or_reset_timer_and_menu_restarts(timer):
    client, sessions, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    with sessions() as session:
        first = session.query(db.ScenarioWait).one()
        ident, due = first.id, first.due_at
    current[0] += timedelta(hours=1)
    client.post("/vk/callback", json=event("Привет снова", 2))
    client.post(
        "/vk/callback", json=event("Кнопка", 3, payload={"button": 0, "node": "old"})
    )
    assert sent.await_count == 1
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().due_at == due
    client.post("/vk/callback", json=event("Меню", 4))
    with sessions() as session:
        assert session.get(db.ScenarioWait, ident).status == "cancelled"
        assert session.query(db.ScenarioWait).filter_by(
            status="pending"
        ).one().due_at == current[0] + timedelta(hours=3)


@pytest.mark.parametrize(
    "action",
    [
        "stop",
        "deny",
        "operator",
        "pause",
        "publish",
        "delete",
        "chat_off",
        "cancel",
        "permission",
        "deactivated",
    ],
)
def test_cancellation_and_permissions(timer, action):
    client, sessions, sent, current, allowed = timer
    flow = install(client)
    client.post("/vk/callback", json=event())
    if action in {"stop", "operator"}:
        client.post(
            "/vk/callback", json=event("Стоп" if action == "stop" else "менеджер", 2)
        )
    elif action == "deny":
        response = client.post(
            "/vk/callback",
            json={
                "type": "message_deny",
                "group_id": 123,
                "secret": "test-secret",
                "object": {"user_id": 77},
            },
        )
        assert response.status_code == 200
    elif action in {"pause", "publish", "delete"}:
        response = client.post(
            f"/admin/api/scenarios/{flow['id']}/{action}",
            json={"revision": flow["revision"]},
        )
        assert response.status_code == 200
    elif action == "cancel":
        assert client.post("/admin/api/conversations/77/cancel-wait").status_code == 200
    elif action == "permission":
        allowed.return_value = False
    else:
        with sessions() as session:
            if action == "chat_off":
                db.save_settings(session, {"chat_enabled": "false"})
            else:
                session.get(db.Client, 77).deactivated = True
                session.commit()
    before = sent.await_count
    current[0] += timedelta(days=1)
    tick()
    assert sent.await_count == before
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"


def test_two_waits_use_distinct_jobs_and_preserve_variables(timer):
    client, sessions, sent, current, _ = timer
    data = wait_graph(1, "seconds")
    data["nodes"][3]["next"] = "wait2"
    data["nodes"].insert(
        -1,
        Graph.model_validate(
            {
                "nodes": [
                    {
                        "id": "wait2",
                        "type": "wait",
                        "delay_value": 2,
                        "delay_unit": "days",
                        "next": "end",
                    }
                ]
            }
        ).model_dump()["nodes"][0],
    )
    data["nodes"][-1]["text"] = "Конец, {first_name}"
    install(client, data)
    client.post("/vk/callback", json=event())
    current[0] += timedelta(seconds=1)
    tick()
    with sessions() as session:
        jobs = session.query(db.ScenarioWait).all()
        assert len(jobs) == 2
        pending = next(j for j in jobs if j.status == "pending")
        assert pending.node_id == "wait2"
        assert pending.due_at == current[0] + timedelta(days=2)
    current[0] += timedelta(days=2)
    tick()
    assert sent.call_args.args[1] == "Конец, Анна"
    assert len({call.kwargs["random_id"] for call in sent.await_args_list}) == 3


def test_transient_retry_has_stable_random_id_and_finite_attempts(timer):
    client, sessions, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    sent.reset_mock()
    sent.side_effect = httpx.ReadTimeout("TEST_SECRET_MUST_NOT_BE_SAVED")
    current[0] += timedelta(hours=3)
    for attempt in range(4):
        tick()
        current[0] += timedelta(minutes=10)
    tick()
    assert sent.await_count == 4
    assert len({call.kwargs["random_id"] for call in sent.await_args_list}) == 1
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert job.status == "failed" and job.attempts == 4
        assert "TEST_SECRET" not in job.error


def test_two_workers_do_not_send_twice(timer):
    client, _, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)

    async def run():
        await asyncio.gather(waits.worker_tick(), waits.worker_tick())

    asyncio.run(run())
    assert sent.await_count == 2


def test_project_isolation_and_pause_never_resurrects_timer(two_projects, monkeypatch):
    from app import projects

    env, first, second = two_projects
    current = [datetime(2026, 9, 29, 10)]
    monkeypatch.setattr(clients, "now", lambda: current[0])
    for project in (first, second):
        route = prefix(project) + "/api/scenarios"
        flow = env.client.post(route).json()
        data = wait_graph()
        data["nodes"][3]["text"] = f"Проект {project.id}"
        flow = env.client.put(
            f"{route}/{flow['id']}",
            json={"title": "Timer", "graph": data, "revision": flow["revision"]},
        ).json()
        assert (
            env.client.post(
                f"{route}/{flow['id']}/publish", json={"revision": flow["revision"]}
            ).status_code
            == 200
        )
        with session_for(project) as session:
            db.save_settings(session, {"chat_enabled": "true"})
        assert callback(env.client, project, message(project)).status_code == 200
    env.network.calls.clear()
    projects.update_project(first.id, enabled=False)
    projects.update_project(first.id, enabled=True)
    current[0] += timedelta(hours=3)

    async def run():
        for project in (first, second):
            with projects.project_scope(project):
                await waits.worker_tick()

    asyncio.run(run())
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert len(sends) == 1
    assert sends[0]["project_id"] == second.id and sends[0]["token"] == second.token
    assert sends[0]["params"]["message"] == f"Проект {second.id}"
    with session_for(first) as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"
    with session_for(second) as session:
        assert session.query(db.ScenarioWait).one().status == "done"


def test_real_storage_upgrade_and_worker_lock(storage, monkeypatch):
    """Runs against both file SQLite and isolated PostgreSQL databases."""
    import weakref
    from app import dialog, projects

    monkeypatch.setattr(dialog, "_locks", weakref.WeakValueDictionary())
    monkeypatch.setattr(dialog, "CALLBACK_SLOTS", asyncio.Semaphore(4))
    (project,) = projects.init_registry()
    sent = AsyncMock()
    monkeypatch.setattr(vk_api, "send_message", sent)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    data = wait_graph()
    with projects.project_scope(project), db.SessionLocal() as session:
        db.save_settings(session, {"chat_enabled": "true"})
        session.add(
            db.Scenario(
                id=1, title="Timer", published=data, draft=data, active=True, version=1
            )
        )
        session.add(db.Client(user_id=77, messages_allowed=True))
        session.add(
            db.Conversation(
                user_id=77,
                scenario_id=1,
                version=1,
                node_id="wait",
                variables={"first_name": "Анна", "_wait_id": "test-wait"},
            )
        )
        session.add(
            db.ScenarioWait(
                id="test-wait",
                user_id=77,
                active_user=77,
                scenario_id=1,
                version=1,
                node_id="wait",
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
                    await waits.worker_tick()  # Must not block the event loop.
                    sent.assert_not_called()
            await asyncio.gather(waits.worker_tick(), waits.worker_tick())

    asyncio.run(run())
    sent.assert_awaited_once()
    with projects.project_scope(project), db.SessionLocal() as session:
        assert session.get(db.ScenarioWait, "test-wait").status == "done"


def test_stop_wins_before_timer_claim_and_retry_can_be_cancelled(timer):
    client, sessions, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)
    sent.side_effect = httpx.ConnectError("offline")
    tick()
    sent.side_effect = None
    client.post("/vk/callback", json=event("Стоп", 2))
    before = sent.await_count
    current[0] += timedelta(minutes=10)
    tick()
    assert sent.await_count == before
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"


def test_worker_shutdown_rolls_back_claim_and_keeps_send_identity(timer):
    client, sessions, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)
    sent.reset_mock()
    sent.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        tick()
    interrupted_id = sent.call_args.kwargs["random_id"]
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert job.status == "pending" and job.attempts == 0
        assert session.get(db.Conversation, 77).node_id == "wait"
    sent.side_effect = None
    tick()
    assert sent.call_args.kwargs["random_id"] == interrupted_id
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "done"


def test_existing_background_worker_services_waits(timer, monkeypatch):
    from app import broadcasts

    client, sessions, sent, current, _ = timer
    install(client)
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)
    monkeypatch.setattr(broadcasts, "refresh_profiles", AsyncMock())
    asyncio.run(broadcasts.worker_tick())
    assert sent.await_count == 2
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "done"


def test_draft_save_keeps_wait_and_latest_wait_is_visible(timer):
    client, sessions, _, _, _ = timer
    flow = install(client)
    client.post("/vk/callback", json=event())
    response = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={
            "title": "Изменённый черновик",
            "graph": wait_graph(1, "days"),
            "revision": flow["revision"],
        },
    )
    assert response.status_code == 200
    result = client.get("/admin/api/conversations").json()[0]["wait"]
    assert result["status"] == "pending" and result["due_at"].endswith("Z")
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "pending"


@pytest.mark.parametrize("change", ["node", "token", "version", "missing_block"])
def test_stale_timer_never_restarts_scenario(timer, change):
    client, sessions, sent, current, _ = timer
    flow = install(client)
    client.post("/vk/callback", json=event())
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        if change == "node":
            row.node_id = "end"
        elif change == "token":
            row.variables = dict(row.variables, _wait_id="another-visit")
        elif change == "version":
            row.version += 1
        else:
            scenario = session.get(db.Scenario, flow["id"])
            scenario.published = {
                "nodes": [n for n in scenario.published["nodes"] if n["id"] != "wait"]
            }
        session.commit()
    current[0] += timedelta(hours=3)
    tick()
    assert sent.await_count == 1
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "cancelled"
