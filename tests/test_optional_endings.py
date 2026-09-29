"""Empty fallback replies and implicit ends; isolated storage, no real VK calls."""

import json
from datetime import timedelta

import pytest

from test_scenarios import setup as setup, event, login, create
from test_waits import timer as timer, tick
from test_scenario_triggers import install
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    callback,
    message,
    session_for,
)
from test_automation_blocks import subflow_graph
from app import db
from app.config import get_settings
from app.flows import Graph, validate_graph


def chain(*nodes, keywords=True):
    return Graph.model_validate(
        {
            "entry": {
                "mode": "keywords" if keywords else "default",
                "keywords": "хочу курс" if keywords else "",
            },
            "nodes": [
                {"id": "start", "type": "start", "next": nodes[0]["id"]},
                *nodes,
            ],
        }
    ).model_dump()


@pytest.mark.parametrize("greeting", ["", " \n\t "])
def test_empty_greeting_is_saved_rendered_and_silent(setup, greeting):
    client, sessions, sent = setup
    login(client)
    result = client.post(
        "/admin/chat-settings", data={"chat_enabled": "1", "chat_greeting": greeting}
    )
    assert result.status_code == 200
    assert result.context["form"]["chat_greeting"] == ""
    with sessions() as session:
        assert db.read_settings(session)["chat_greeting"] == ""
        assert db.read_settings(session)["chat_enabled"] == "true"
    for number, value in enumerate(("привет", "меню", "начать"), 1):
        assert client.post("/vk/callback", json=event(value, number)).text == "ok"
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.DialogEvent).filter_by(status="done").count() == 3


def test_unset_greeting_keeps_legacy_default_and_nonempty_still_sends(setup):
    client, _, sent = setup
    client.post("/vk/callback", json=event("привет"))
    assert sent.call_args.args[1] == get_settings().chat_greeting
    login(client)
    client.post(
        "/admin/chat-settings",
        data={"chat_enabled": "1", "chat_greeting": "  Новый ответ  "},
    )
    client.post("/vk/callback", json=event("привет", 2))
    assert sent.call_args.args[1] == "Новый ответ"


def test_silent_fallback_does_not_disable_keyword_restarts(setup):
    client, sessions, sent = setup
    install(client, chain({"id": "hello", "type": "message", "text": "Курс"}))
    client.post("/admin/chat-settings", data={"chat_enabled": "1", "chat_greeting": ""})
    for number, value in enumerate(
        ("привет", "хочу курс", "не подходит", "Хочу курс!"), 1
    ):
        assert client.post("/vk/callback", json=event(value, number)).text == "ok"
        if number == 2:
            client.post(
                "/vk/callback", json=event(value, number)
            )  # A duplicate, not a restart.
    assert [call.args[1] for call in sent.call_args_list] == ["Курс", "Курс"]
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and row.variables["_nonce"] == ""


def test_empty_greeting_is_project_local(two_projects):
    env, first, second = two_projects
    result = env.client.post(
        prefix(first) + "/chat-settings",
        data={"chat_enabled": "1", "chat_greeting": ""},
    )
    assert result.status_code == 200
    assert result.context["form"]["chat_greeting"] == ""
    for project in (first, second):
        callback(env.client, project, message(project))
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert len(sends) == 1 and sends[0]["project_id"] == second.id
    with session_for(first) as session:
        assert db.read_settings(session)["chat_greeting"] == ""
    with session_for(second) as session:
        assert "chat_greeting" not in db.read_settings(session)


@pytest.mark.parametrize(
    "node",
    [
        {"type": "message", "text": "Сообщение"},
        {"type": "random", "variants": ["Первый", "Второй"]},
        {"type": "set_variable", "variable": "saved", "value": "yes"},
        {"type": "tag", "tag": "Курс"},
        {"type": "condition", "words": "курс"},
        {"type": "variable_condition", "variable": "answer", "comparison": "empty"},
        {"type": "phone_condition"},
        {"type": "schedule"},
    ],
)
def test_terminal_nodes_publish_and_preview_without_end(setup, node):
    client, _, sent = setup
    data = chain(dict(node, id="last"))
    assert validate_graph(data) == []
    install(client, data)
    result = client.post("/admin/api/preview", json={"graph": data, "text": "курс"})
    assert result.status_code == 200, result.text
    assert result.json()["state"]["node_id"] == ""
    assert result.json()["state"]["stack"] == []
    sent.assert_not_called()


@pytest.mark.parametrize("kind", ["question", "contact"])
def test_terminal_question_and_contact_wait_then_finish(timer, kind):
    client, sessions, sent, _, _ = timer
    data = chain(
        {
            "id": "ask",
            "type": kind,
            "text": "Оставьте ответ",
            "variable": "phone",
            "allow_skip": False,
            "reminder_enabled": True,
        }
    )
    install(client, data)
    client.post("/vk/callback", json=event("хочу курс"))
    assert sent.call_count == 1
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "ask"
    client.post("/vk/callback", json=event("89991234567", 2))
    assert sent.call_count == 1  # Save the answer without a mandatory final message.
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and row.variables["phone"]
        assert "_wait_id" not in row.variables
        if kind == "contact":
            assert session.get(db.Client, 77).phone == "+79991234567"
            assert session.query(db.ScenarioWait).one().status == "cancelled"
    client.post("/vk/callback", json=event("хочу курс", 3))
    assert sent.call_count == 2


def test_terminal_contact_keeps_later_and_silence_reminders(timer):
    client, sessions, sent, current, _ = timer
    install(
        client,
        chain(
            {
                "id": "ask",
                "type": "contact",
                "text": "Телефон?",
                "variable": "phone",
                "allow_skip": False,
                "allow_later": True,
                "later_text": "Можно позже",
                "reminder_enabled": True,
                "reminder_delay_value": 1,
                "reminder_delay_unit": "seconds",
                "reminder_text": "Жду телефон",
                "later_reminder_enabled": True,
                "later_reminder_delay_value": 1,
                "later_reminder_delay_unit": "seconds",
                "later_reminder_text": "Напоминаю после Позже",
            }
        ),
    )
    client.post("/vk/callback", json=event("хочу курс"))
    client.post("/vk/callback", json=event("ошибка", 2))
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "ask"
    current[0] += timedelta(seconds=2)
    tick()
    assert sent.call_args.args[1] == "Жду телефон"
    client.post("/vk/callback", json=event("Позже", 3))
    current[0] += timedelta(seconds=2)
    tick()
    assert sent.call_args.args[1] == "Напоминаю после Позже"
    before = sent.call_count
    client.post("/vk/callback", json=event("89991234567", 4))
    assert sent.call_count == before
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == ""
        assert session.query(db.ScenarioWait).filter_by(status="pending").count() == 0


@pytest.mark.parametrize(
    "kind,answer", [("wait", False), ("wait_reply", False), ("wait_reply", True)]
)
def test_terminal_wait_completes_after_timer_or_reply(timer, kind, answer):
    client, sessions, sent, current, _ = timer
    install(
        client,
        chain(
            {
                "id": "wait",
                "type": kind,
                "text": "Ответьте",
                "variable": "reply",
                "delay_value": 1,
                "delay_unit": "seconds",
            }
        ),
    )
    client.post("/vk/callback", json=event("хочу курс"))
    before = sent.call_count
    if answer:
        client.post("/vk/callback", json=event("Ответ", 2))
    else:
        current[0] += timedelta(seconds=2)
        tick()
    assert sent.call_count == before
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and "_wait_id" not in row.variables
        assert session.query(db.ScenarioWait).one().status == "done"
        if answer:
            assert row.variables["reply"] == "Ответ"


def test_button_can_finish_without_an_end_block(setup):
    client, sessions, sent = setup
    install(
        client,
        chain(
            {
                "id": "buttons",
                "type": "message",
                "text": "Выберите",
                "buttons": [{"kind": "next", "label": "Готово", "target": ""}],
            }
        ),
    )
    client.post("/vk/callback", json=event("хочу курс"))
    payload = json.loads(
        sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"]
    )
    client.post("/vk/callback", json=event("Готово", 2, payload=payload))
    assert sent.call_count == 1
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == ""


def test_link_only_message_does_not_leave_dialogue_waiting(setup):
    client, sessions, sent = setup
    install(
        client,
        chain(
            {
                "id": "link",
                "type": "message",
                "text": "Открыть сайт",
                "buttons": [
                    {"kind": "link", "label": "Сайт", "url": "https://example.com"}
                ],
            }
        ),
    )
    client.post("/vk/callback", json=event("хочу курс"))
    assert (
        sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["type"]
        == "open_link"
    )
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == ""


def test_subflow_return_can_finish_main_but_not_drop_outer_return(setup):
    client, sessions, sent = setup
    data = subflow_graph()
    data["nodes"] = [n for n in data["nodes"] if n["id"] != "done"]
    next(n for n in data["nodes"] if n["id"] == "call")["next"] = ""
    assert validate_graph(data) == []
    install(client, data)
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("Москва", 2))
    assert sent.call_count == 1
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.node_id == "" and row.variables["_stack"] == []
        assert row.variables["city"] == "Москва"
    next(n for n in data["nodes"] if n["id"] == "city")["next"] = ""
    assert any("Возврат" in error for error in validate_graph(data))


def test_missing_ids_and_empty_start_or_callee_are_still_invalid():
    data = chain({"id": "last", "type": "message", "text": "Текст", "next": "missing"})
    assert any("выберите следующий блок" in error for error in validate_graph(data))
    data["nodes"][0]["next"] = ""
    data["nodes"][0]["title"] = "Проверка входа"
    assert any("Проверка входа → Далее" in error for error in validate_graph(data))
    assert validate_graph(chain({"id": "call", "type": "call_subflow"}))


def test_hundred_node_graph_allows_implicit_final_step(setup):
    client, _, _ = setup
    create(client)
    data = chain(
        *[
            {
                "id": f"m{i}",
                "type": "message",
                "text": "Текст",
                "next": f"m{i + 1}" if i < 98 else "",
            }
            for i in range(99)
        ]
    )
    result = client.post("/admin/api/preview", json={"graph": data})
    assert result.status_code == 200, result.text
    assert len(result.json()["messages"]) == 99
    assert result.json()["state"]["node_id"] == ""
