import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import httpx
from sqlalchemy import text as sql_text

from test_scenarios import setup as setup, create, event
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
from app.flows import Graph, validate_graph
from app.flow_rules import in_schedule, normalize_tag


def reply_graph():
    return graph(
        {
            "id": "ask",
            "type": "wait_reply",
            "text": "Какой цвет?",
            "variable": "color",
            "delay_value": 3,
            "delay_unit": "hours",
            "yes": "answer",
            "no": "timeout",
        },
        {"id": "answer", "type": "end", "text": "Ваш цвет: {color}"},
        {"id": "timeout", "type": "end", "text": "Ответ не получен"},
    )


def subflow_graph(*, wait=False):
    return graph(
        {
            "id": "private",
            "type": "set_variable",
            "variable": "private",
            "value": "Не передавать",
            "next": "call",
        },
        {
            "id": "call",
            "type": "call_subflow",
            "subflow_id": "delivery",
            "return_variables": ["city"],
            "next": "done",
        },
        {"id": "done", "type": "end", "text": "Город: {city}, своё: {private}"},
        {"id": "delivery", "type": "subflow", "title": "Доставка", "next": "city"},
        {
            "id": "city",
            "type": "question",
            "variable": "city",
            "text": "Город, {first_name}?",
            "next": "wait" if wait else "back",
        },
        *(
            [
                {
                    "id": "wait",
                    "type": "wait",
                    "delay_value": 1,
                    "delay_unit": "seconds",
                    "next": "back",
                }
            ]
            if wait
            else []
        ),
        {"id": "back", "type": "return"},
    )


def schedule_node(**values):
    return Graph.model_validate(
        {"nodes": [{"id": "schedule", "type": "schedule", **values}]}
    ).model_dump()["nodes"][0]


@pytest.mark.parametrize(
    "instant, expected",
    [
        ("2026-09-28T05:59:59+00:00", False),
        ("2026-09-28T06:00:00+00:00", True),
        ("2026-09-28T14:59:59+00:00", True),
        ("2026-09-28T15:00:00+00:00", False),
        ("2026-10-03T09:00:00+00:00", False),
    ],
)
def test_schedule_boundaries(instant, expected):
    assert in_schedule(schedule_node(), datetime.fromisoformat(instant)) is expected


def test_overnight_and_date_window_belong_to_shift_start():
    node = schedule_node(
        weekdays=[4],
        time_from="22:00",
        time_to="06:00",
        date_from="2026-10-02",
        date_to="2026-10-02",
    )
    assert in_schedule(node, datetime.fromisoformat("2026-10-02T19:00:00+00:00"))
    assert in_schedule(node, datetime.fromisoformat("2026-10-03T02:59:59+00:00"))
    assert not in_schedule(node, datetime.fromisoformat("2026-10-03T03:00:00+00:00"))
    assert not in_schedule(node, datetime.fromisoformat("2026-10-03T20:00:00+00:00"))


def test_dst_and_all_day():
    node = schedule_node(
        timezone="America/New_York", weekdays=[6], time_from="01:00", time_to="02:00"
    )
    for when in ("2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"):
        assert in_schedule(node, datetime.fromisoformat(when))
    all_day = schedule_node(
        timezone="UTC",
        weekdays=[0, 1, 2, 3, 4, 5, 6],
        time_from="00:00",
        time_to="24:00",
    )
    assert in_schedule(all_day, datetime(2026, 9, 29, 23, 59, 59, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "fields",
    [
        {"weekdays": []},
        {"weekdays": [0, 0]},
        {"time_from": "24:00"},
        {"time_to": "25:00"},
        {"time_from": "09:00", "time_to": "09:00"},
        {"timezone": "Bogus/Zone"},
        {"timezone": "../../etc/passwd"},
        {"date_from": "2026-02-30"},
        {"date_from": "2026-12-01", "date_to": "2026-01-01"},
    ],
)
def test_bad_schedule_cannot_publish(fields):
    data = graph(
        {**schedule_node(**fields), "yes": "end", "no": "end"},
        {"id": "end", "type": "end"},
    )
    assert validate_graph(data)


def test_preview_schedule_and_tags_never_change_customers(timer):
    client, sessions, sent, _, allowed = timer
    create(client)
    data = graph(
        {"id": "add", "type": "tag", "tag": " Интерес ", "next": "check"},
        {
            "id": "check",
            "type": "tag_condition",
            "tag": "интерес",
            "yes": "schedule",
            "no": "closed",
        },
        {"id": "schedule", "type": "schedule", "yes": "open", "no": "closed"},
        {"id": "open", "type": "end", "text": "Открыто"},
        {"id": "closed", "type": "end", "text": "Закрыто"},
    )
    assert not validate_graph(data)
    for hour, expected in ((9, "Открыто"), (23, "Закрыто")):
        response = client.post(
            "/admin/api/preview",
            json={
                "graph": data,
                "simulated_at": f"2026-09-29T{hour:02}:00:00Z",
                "tags": ["клиент"],
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["state"]["tags"] == ["интерес", "клиент"]
        assert result["messages"][0]["text"] == expected
    with sessions() as session:
        assert session.query(db.Client).count() == 0
        assert session.query(db.ScenarioWait).count() == 0
    sent.assert_not_called()
    allowed.assert_not_called()


def test_live_tags_survive_menu_and_can_be_removed(timer):
    client, sessions, sent, _, _ = timer
    data = graph(
        {
            "id": "check",
            "type": "tag_condition",
            "tag": "VIP",
            "yes": "remove",
            "no": "add",
        },
        {"id": "add", "type": "tag", "tag": "vip", "next": "done"},
        {
            "id": "remove",
            "type": "tag",
            "tag_action": "remove",
            "tag": "vip",
            "next": "done",
        },
        {"id": "done", "type": "end", "text": "Готово"},
    )
    install(client, data)
    client.post("/vk/callback", json=event())
    with sessions() as session:
        assert session.get(db.Client, 77).tags == ["vip"]
    assert client.get("/admin/api/clients/77").json()["client"]["tags"] == ["vip"]
    client.post("/vk/callback", json=event("Меню", 2))
    with sessions() as session:
        assert session.get(db.Client, 77).tags == []
        assert not session.get(db.Client, 77).unsubscribed
    assert sent.await_count == 2


def test_reply_wins_and_timer_stays_done_after_restart(timer):
    client, sessions, sent, current, _ = timer
    install(client, reply_graph())
    client.post("/vk/callback", json=event())
    assert sent.call_args.args[1] == "Какой цвет?"
    current[0] += timedelta(hours=2)
    client.post("/vk/callback", json=event("Синий", 2))
    assert sent.call_args.args[1] == "Ваш цвет: Синий"
    current[0] += timedelta(hours=2)
    db.init_db()
    tick()
    assert sent.await_count == 2
    with sessions() as session:
        assert session.query(db.ScenarioWait).one().status == "done"
        assert session.get(db.Conversation, 77).variables["color"] == "Синий"


def test_no_reply_and_late_reply_cannot_choose_yes(timer):
    client, sessions, sent, current, _ = timer
    install(client, reply_graph())
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)
    client.post("/vk/callback", json=event("Поздний ответ", 2))
    assert "Время для ответа" in sent.call_args.args[1]
    tick()
    assert sent.call_args.args[1] == "Ответ не получен"
    assert all("Ваш цвет" not in call.args[1] for call in sent.await_args_list)
    with sessions() as session:
        assert "color" not in session.get(db.Conversation, 77).variables


def test_empty_and_stale_buttons_do_not_reschedule_reply(timer):
    client, sessions, sent, _, _ = timer
    install(client, reply_graph())
    client.post("/vk/callback", json=event())
    with sessions() as session:
        due = session.query(db.ScenarioWait).one().due_at
    client.post("/vk/callback", json=event("", 2))
    client.post(
        "/vk/callback",
        json=event("Старая кнопка", 3, payload={"button": 0, "node": "old"}),
    )
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert job.due_at == due and job.status == "pending"
    assert "ответьте текстом" in sent.call_args.args[1]


def test_preview_reply_or_simulated_timeout(timer):
    client, _, sent, _, _ = timer
    create(client)
    data = reply_graph()
    first = client.post(
        "/admin/api/preview",
        json={"graph": data, "simulated_at": "2026-09-29T10:00:00Z"},
    ).json()
    assert first["state"]["waiting"]["deadline"] == "2026-09-29T13:00:00+00:00"
    for params, expected in (
        ({"text": "Белый"}, "Ваш цвет: Белый"),
        ({"resume_wait": True}, "Ответ не получен"),
    ):
        result = client.post(
            "/admin/api/preview",
            json={"graph": data, "state": first["state"], **params},
        ).json()
        assert result["messages"][0]["text"] == expected
    sent.assert_not_called()


@pytest.mark.parametrize("wait", [False, True])
def test_subflow_keeps_return_stack_across_replies_and_timers(timer, wait):
    client, sessions, sent, current, _ = timer
    data = subflow_graph(wait=wait)
    assert not validate_graph(data)
    install(client, data)
    client.post("/vk/callback", json=event())
    with sessions() as session:
        variables = session.get(db.Conversation, 77).variables
        assert "private" not in variables
        assert len(variables["_stack"]) == 1
    client.post("/vk/callback", json=event("Москва", 2))
    if wait:
        current[0] += timedelta(seconds=1)
        db.init_db()
        tick()
    assert sent.call_args.args[1] == "Город: Москва, своё: Не передавать"
    with sessions() as session:
        saved = session.get(db.Conversation, 77).variables
        assert saved["_stack"] == []
        assert saved["last_message"] == "Москва"


def test_subflow_cycles_crossings_and_invalid_returns_are_rejected():
    data = subflow_graph()
    data["nodes"][1]["next"] = "delivery"
    assert any("пересекаются" in e for e in validate_graph(data))
    data = subflow_graph()
    data["nodes"][-1]["type"] = "end"
    assert any("Возврат" in e for e in validate_graph(data))
    data = graph({"id": "return", "type": "return"})
    assert any("Возврат" in e for e in validate_graph(data))
    data = subflow_graph()
    data["nodes"][-1].update(type="call_subflow", subflow_id="delivery", next="city")
    assert any("по кругу" in e for e in validate_graph(data))


def test_reply_timeout_loops_forbidden_but_answer_loop_allowed():
    data = reply_graph()
    data["nodes"][1]["no"] = "ask"
    assert any("цикл" in e for e in validate_graph(data))
    data = reply_graph()
    data["nodes"][1]["yes"] = "ask"
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "answer"]
    assert not validate_graph(data)


def test_tags_validate_without_silent_truncation():
    assert normalize_tag("  Новый   КЛИЕНТ ") == "новый клиент"
    for tag in ("", "<script>", "a" * 41):
        with pytest.raises(ValueError):
            normalize_tag(tag)


def test_timeout_retry_never_extends_answer_deadline(timer):
    client, sessions, sent, current, _ = timer
    install(client, reply_graph())
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=3)
    deadline = current[0]
    sent.side_effect = httpx.ReadTimeout("test timeout")
    tick()
    retry_random_id = sent.call_args.kwargs["random_id"]
    sent.side_effect = None
    with sessions() as session:
        job = session.query(db.ScenarioWait).one()
        assert job.expires_at == deadline
        assert job.due_at > deadline and job.attempts == 1
    current[0] += timedelta(seconds=1)
    client.post("/vk/callback", json=event("Синий", 2))
    assert "Время для ответа" in sent.call_args.args[1]
    current[0] += timedelta(seconds=30)
    tick()
    assert sent.call_args.args[1] == "Ответ не получен"
    assert sent.call_args.kwargs["random_id"] == retry_random_id
    assert not any(c.args[1].startswith("Ваш цвет:") for c in sent.await_args_list)


@pytest.mark.parametrize("command", ["Стоп", "менеджер", "Меню"])
def test_reply_wait_can_be_cancelled_or_restarted(timer, command):
    client, sessions, sent, current, _ = timer
    install(client, reply_graph())
    client.post("/vk/callback", json=event())
    current[0] += timedelta(hours=1)
    client.post("/vk/callback", json=event(command, 2))
    before = sent.await_count
    current[0] += timedelta(hours=2)
    tick()
    assert sent.await_count == before
    with sessions() as session:
        assert session.query(db.ScenarioWait).filter_by(status="cancelled").count() == 1
        assert session.query(db.ScenarioWait).filter_by(status="pending").count() == (
            command == "Меню"
        )


def nested_subflows(depth):
    nodes = [
        {"id": "call0", "type": "call_subflow", "subflow_id": "sub0", "next": "done"},
        {"id": "done", "type": "end", "text": "Готово"},
    ]
    for index in range(depth):
        nodes += [
            {
                "id": f"sub{index}",
                "type": "subflow",
                "next": f"back{index}" if index == depth - 1 else f"call{index + 1}",
            },
            {"id": f"back{index}", "type": "return"},
        ]
        if index < depth - 1:
            nodes.append(
                {
                    "id": f"call{index + 1}",
                    "type": "call_subflow",
                    "subflow_id": f"sub{index + 1}",
                    "next": f"back{index}",
                }
            )
    return graph(*nodes)


def test_five_nested_subchains_execute_six_rejected(timer):
    client, _, _, _, _ = timer
    create(client)
    data = nested_subflows(5)
    assert not validate_graph(data)
    result = client.post("/admin/api/preview", json={"graph": data})
    assert result.status_code == 200, result.text
    assert result.json()["messages"][0]["text"] == "Готово"
    assert result.json()["state"]["stack"] == []
    assert any("Вложенность" in issue for issue in validate_graph(nested_subflows(6)))


def test_subchain_variables_are_explicit_and_reserved_outputs_rejected(timer):
    client, _, _, _, _ = timer
    create(client)
    data = subflow_graph()
    first = client.post("/admin/api/preview", json={"graph": data}).json()
    assert "private" not in first["state"]["variables"]
    data["nodes"][2]["pass_variables"] = ["private"]
    second = client.post("/admin/api/preview", json={"graph": data}).json()
    assert second["state"]["variables"]["private"] == "Не передавать"
    data["nodes"][2]["return_variables"] = ["first_name"]
    assert any("служебные" in error for error in validate_graph(data))


def test_tags_and_reply_waits_are_project_local(two_projects, monkeypatch):
    env, first, second = two_projects
    current = [datetime(2026, 9, 29, 10)]
    monkeypatch.setattr(clients, "now", lambda: current[0])
    for project in (first, second):
        route = prefix(project) + "/api/scenarios"
        flow = env.client.post(route).json()
        data = reply_graph()
        data["nodes"][0]["next"] = "tag"
        data = graph(
            *data["nodes"][1:],
            {"id": "tag", "type": "tag", "tag": f"проект {project.id}", "next": "ask"},
        )
        data["nodes"][0]["next"] = "tag"
        flow = env.client.put(
            f"{route}/{flow['id']}",
            json={
                "title": "Tags and reply",
                "graph": data,
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
        assert callback(env.client, project, message(project)).status_code == 200
        with session_for(project) as session:
            assert session.get(db.Client, 77).tags == [f"проект {project.id}"]
            assert session.query(db.ScenarioWait).one().status == "pending"
    projects.update_project(first.id, enabled=False)
    current[0] += timedelta(hours=3)
    env.network.calls.clear()

    async def run():
        for project in (first, second):
            with projects.project_scope(project):
                await waits.worker_tick()

    asyncio.run(run())
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert len(sends) == 1 and sends[0]["project_id"] == second.id
    assert sends[0]["params"]["message"] == "Ответ не получен"


def test_additive_columns_keep_old_customer_and_timer(storage):
    with db.SessionLocal() as session:
        session.add(
            db.Client(user_id=77, first_name="Не потерять", phone="+79990000000")
        )
        session.add(
            db.ScenarioWait(
                id="old-wait",
                user_id=77,
                active_user=77,
                scenario_id=1,
                version=1,
                node_id="wait",
                due_at=datetime(2026, 10, 1),
            )
        )
        session.commit()
    # These are disposable fixture databases, not an installed application.
    with storage.engine.begin() as connection:
        connection.execute(sql_text("ALTER TABLE clients DROP COLUMN tags"))
        connection.execute(
            sql_text("ALTER TABLE scenario_waits DROP COLUMN expires_at")
        )
    db.init_db()
    db.init_db()
    with db.SessionLocal() as session:
        customer = session.get(db.Client, 77)
        assert customer.first_name == "Не потерять" and customer.phone == "+79990000000"
        assert customer.tags == []
        job = session.get(db.ScenarioWait, "old-wait")
        assert job.due_at == datetime(2026, 10, 1) and job.status == "pending"
        assert job.expires_at is None


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("worker_first", [False, True])
def test_reply_decision_and_timeout_on_real_storage(
    storage, monkeypatch, late, worker_first
):
    import weakref

    monkeypatch.setattr(dialog, "_locks", weakref.WeakValueDictionary())
    monkeypatch.setattr(dialog, "CALLBACK_SLOTS", asyncio.Semaphore(4))
    (project,) = projects.init_registry()

    async def yield_send(*args, **kwargs):
        await asyncio.sleep(0)

    sent = AsyncMock(side_effect=yield_send)
    monkeypatch.setattr(vk_api, "send_message", sent)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    data = reply_graph()
    current = [datetime(2026, 9, 29, 10)]
    monkeypatch.setattr(clients, "now", lambda: current[0])
    with projects.project_scope(project), db.SessionLocal() as session:
        db.save_settings(session, {"chat_enabled": "true"})
        session.add(
            db.Scenario(
                id=1, title="Reply", published=data, draft=data, active=True, version=1
            )
        )
        session.add(db.Client(user_id=77, messages_allowed=True))
        session.add(
            db.Conversation(
                user_id=77,
                scenario_id=1,
                version=1,
                node_id="ask",
                variables={"first_name": "Анна", "_wait_id": "reply"},
            )
        )
        session.add(
            db.ScenarioWait(
                id="reply",
                user_id=77,
                active_user=77,
                scenario_id=1,
                version=1,
                node_id="ask",
                due_at=current[0] + timedelta(hours=3),
                expires_at=current[0] + timedelta(hours=3),
            )
        )
        session.commit()
    current[0] += timedelta(hours=3, seconds=0 if late else -1)

    async def run():
        with projects.project_scope(project):
            # Separate file/PG connections are required; in-memory StaticPool
            # fixtures share a single connection and cannot isolate transactions.
            tasks = [dialog.handle_message(event("Красный", 2)), waits.worker_tick()]
            await asyncio.gather(*(tasks[::-1] if worker_first else tasks))

    asyncio.run(run())
    texts = [call.args[1] for call in sent.await_args_list]
    assert texts.count("Ответ не получен") == int(late)
    assert texts.count("Ваш цвет: Красный") == int(not late)
    with projects.project_scope(project), db.SessionLocal() as session:
        assert session.get(db.ScenarioWait, "reply").status == "done"
