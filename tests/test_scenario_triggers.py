"""Keyword routing uses published, project-local graphs; no live VK calls."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from test_scenarios import setup as setup, create, event, publish
from test_waits import timer as timer, wait_graph, tick
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    callback,
    message,
    session_for,
)
from test_project_storage import storage as storage
from app import db
from app.flows import Graph, starter_graph, validate_graph
from app.scenario_triggers import choose_keyword, EntryRule


def graph(title="Курс", keywords="хочу курс", match="contains"):
    return Graph.model_validate(
        {
            "entry": {
                "mode": "default" if keywords is None else "keywords",
                "keywords": keywords or "",
                "match": match,
            },
            "nodes": [
                {"id": "start", "type": "start", "next": "question"},
                {
                    "id": "question",
                    "type": "question",
                    "text": title,
                    "variable": "reply",
                    "next": "end",
                },
                {"id": "end", "type": "end", "text": title + ": {reply}"},
            ],
        }
    ).model_dump()


def install(client, data=None, route="/admin/api/scenarios"):
    if route == "/admin/api/scenarios":
        row = create(client)
    else:
        row = client.post(route).json()
    row = client.put(
        f"{route}/{row['id']}",
        json={
            "title": "Тест",
            "revision": row["revision"],
            "graph": data or graph(),
        },
    )
    assert row.status_code == 200, row.text
    row = row.json()
    result = client.post(
        f"{route}/{row['id']}/publish", json={"revision": row["revision"]}
    )
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize(
    "text,rule,mode,expected",
    [
        ("ХОЧУ КУРС!", "хочу курс", "contains", True),
        ("Добрый день, хочу   курс ❤️", "хочу курс", "contains", True),
        ("ХОЧУ КУРС!", "хочу курс", "exact", True),
        ("Добрый день, хочу курс", "хочу курс", "exact", False),
        ("курсы", "курс", "contains", False),
        ("маникурс", "курс", "contains", False),
        ("хочу маникюр", "курс", "contains", False),
        ("расчёт", "расчет", "exact", True),
        ("Хочу\nкурс", "хочу курс", "exact", True),
        ("подарок", "хочу курс, подарок; помощь", "exact", True),
        ("", "курс", "contains", False),
        ("что угодно", "", "contains", False),
    ],
)
def test_matching(text, rule, mode, expected):
    item = SimpleNamespace(id=1, published=graph(keywords=rule, match=mode))
    assert (choose_keyword([item], text) is item) is expected


def test_specific_rule_wins_and_ties_are_stable():
    broad = SimpleNamespace(id=1, published=graph(keywords="курс"))
    longer = SimpleNamespace(id=2, published=graph(keywords="хочу курс"))
    exact = SimpleNamespace(id=3, published=graph(keywords="хочу курс", match="exact"))
    assert choose_keyword([broad, longer], "Хочу курс!") is longer
    assert choose_keyword([broad, longer, exact], "Хочу курс!") is exact
    assert choose_keyword([exact, longer], "Хочу курс!") is exact


@pytest.mark.parametrize(
    "keywords",
    [
        "",
        " \n,;",
        "!!!",
        "меню",
        "/start",
        "СТОП",
        ",".join(f"фраза{i}" for i in range(31)),
    ],
)
def test_invalid_entry_cannot_publish(setup, keywords):
    client, _, _ = setup
    row = create(client)
    data = graph(keywords=keywords)
    assert validate_graph(data)
    saved = client.put(
        f"/admin/api/scenarios/{row['id']}",
        json={
            "title": "Черновик",
            "graph": data,
            "revision": row["revision"],
        },
    )
    assert saved.status_code == 200
    result = client.post(
        f"/admin/api/scenarios/{row['id']}/publish",
        json={"revision": saved.json()["revision"]},
    )
    assert result.status_code == 422


def test_entry_schema_and_legacy_defaults():
    for value in ({"mode": "anything"}, {"match": "regex"}, {"keywords": "x" * 2001}):
        with pytest.raises(ValidationError):
            EntryRule(**value)
    legacy = starter_graph()
    legacy.pop("entry")
    assert not validate_graph(legacy)
    assert Graph.model_validate(legacy).entry.mode == "default"


def test_multiple_live_entries_and_continuation(setup):
    client, sessions, sent = setup
    default = install(client, graph("Обычный диалог", None))
    course = install(client)
    product = install(client, graph("Товары", "хочу товар"))
    with sessions() as session:
        assert session.query(db.Scenario).filter_by(active=True).count() == 3
    for text, number, expected in [
        ("ХОЧУ КУРС!", 1, "Курс"),
        ("Анна", 2, "Курс: Анна"),
        ("привет", 3, "Обычный диалог"),
        ("Я хочу товар", 4, "Товары"),
        ("меню", 5, "Обычный диалог"),
    ]:
        assert client.post("/vk/callback", json=event(text, number)).text == "ok"
        assert sent.call_args.args[1] == expected
    with sessions() as session:
        assert session.get(db.Conversation, 77).scenario_id == default["id"]
        assert session.get(db.Scenario, course["id"]).active
        assert session.get(db.Scenario, product["id"]).active


def test_keyword_only_never_starts_without_match(setup):
    client, sessions, sent = setup
    row = install(client, graph(match="exact"))
    client.post("/vk/callback", json=event("я хочу курс"))
    assert sent.call_args.args[1] != "Курс"
    client.post("/vk/callback", json=event("хочу курс", 2))
    assert sent.call_args.args[1] == "Курс"
    with sessions() as session:
        assert session.get(db.Conversation, 77).scenario_id == row["id"]


def test_keyword_restart_dedup_and_no_cross_user_state(setup):
    client, sessions, sent = setup
    install(client)
    client.post("/vk/callback", json=event("хочу курс"))
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        row.variables = dict(row.variables, old_answer="old")
        session.commit()
    client.post("/vk/callback", json=event("хочу курс", 2))
    assert sent.call_count == 2
    client.post("/vk/callback", json=event("хочу курс", 2))
    assert sent.call_count == 2
    with sessions() as session:
        assert "old_answer" not in session.get(db.Conversation, 77).variables
    client.post("/vk/callback", json=event("хочу курс", user=88))
    client.post("/vk/callback", json=event("Ответ Анны", 3))
    with sessions() as session:
        assert session.get(db.Conversation, 88).node_id == "question"
        assert session.get(db.Conversation, 77).variables["reply"] == "Ответ Анны"


def test_conflicting_keyword_publish_is_atomic(setup):
    client, sessions, _ = setup
    first = install(client)
    row = create(client)
    saved = client.put(
        f"/admin/api/scenarios/{row['id']}",
        json={
            "title": "Конфликт",
            "graph": graph(keywords="ХОЧУ   КУРС!"),
            "revision": row["revision"],
        },
    ).json()
    response = client.post(
        f"/admin/api/scenarios/{row['id']}/publish",
        json={"revision": saved["revision"]},
    )
    assert response.status_code == 422
    assert "уже используется" in response.text
    with sessions() as session:
        assert session.get(db.Scenario, first["id"]).active
        assert not session.get(db.Scenario, row["id"]).active
        assert session.get(db.Scenario, row["id"]).published is None


def test_draft_does_not_change_keyword_routing(setup):
    client, _, sent = setup
    row = install(client)
    data = copy.deepcopy(row["graph"])
    data["entry"]["keywords"] = "хочу обучение"
    saved = client.put(
        f"/admin/api/scenarios/{row['id']}",
        json={
            "title": "Новый запуск",
            "graph": data,
            "revision": row["revision"],
        },
    ).json()
    client.post("/vk/callback", json=event("хочу обучение", user=88))
    assert sent.call_args.args[1] != "Курс"
    client.post("/vk/callback", json=event("хочу курс"))
    assert sent.call_args.args[1] == "Курс"
    publish(client, saved)
    client.post("/vk/callback", json=event("хочу обучение", 2, user=88))
    assert sent.call_args.args[1] == "Курс"


def test_paused_deleted_and_disabled_chat_do_not_launch(setup):
    client, sessions, sent = setup
    row = install(client)
    client.post(
        f"/admin/api/scenarios/{row['id']}/pause", json={"revision": row["revision"]}
    )
    client.post("/vk/callback", json=event("хочу курс"))
    assert sent.call_args.args[1] != "Курс"
    row = install(client)
    client.post(
        f"/admin/api/scenarios/{row['id']}/delete", json={"revision": row["revision"]}
    )
    client.post("/vk/callback", json=event("хочу курс", 2))
    assert sent.call_args.args[1] != "Курс"
    install(client)
    with sessions() as session:
        db.save_settings(session, {"chat_enabled": "0"})
        session.commit()
    before = sent.call_count
    client.post("/vk/callback", json=event("хочу курс", 3))
    assert sent.call_count == before


def test_manager_commands_and_button_payload_take_priority(setup):
    client, sessions, sent = setup
    default = install(client, starter_graph())
    course = install(client, graph(keywords="хочу курс, Подобрать товар, оператор"))
    client.post("/vk/callback", json=event("привет"))
    payload = json.loads(
        sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"]
    )
    client.post("/vk/callback", json=event("Подобрать товар", 2, payload=payload))
    with sessions() as session:
        assert session.get(db.Conversation, 77).scenario_id == default["id"]
    client.post("/vk/callback", json=event("оператор", 3))
    with sessions() as session:
        assert session.get(db.Conversation, 77).handoff
    before = sent.call_count
    client.post("/vk/callback", json=event("хочу курс", 4))
    assert sent.call_count == before
    client.post("/vk/callback", json=event("меню", 5))
    client.post("/vk/callback", json=event("хочу курс", 6))
    with sessions() as session:
        assert session.get(db.Conversation, 77).scenario_id == course["id"]
    client.post("/vk/callback", json=event("Стоп", 7))
    assert "Рассылки отключены" in sent.call_args.args[1]


def test_keywords_cancel_only_this_users_wait_and_publication_is_scoped(timer):
    client, sessions, sent, current, _ = timer
    waiting = install(client, wait_graph())
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event(user=88))
    course = install(client)
    with sessions() as session:
        assert session.query(db.ScenarioWait).filter_by(status="pending").count() == 2
    client.post("/vk/callback", json=event("хочу курс", 2))
    with sessions() as session:
        assert (
            session.query(db.ScenarioWait).filter_by(user_id=77).one().status
            == "cancelled"
        )
        assert (
            session.query(db.ScenarioWait).filter_by(user_id=88).one().status
            == "pending"
        )
        assert session.get(db.Conversation, 77).scenario_id == course["id"]
    # Re-publishing a keyword scenario cannot cancel a default scenario's timers.
    publish(client, course)
    from datetime import timedelta

    current[0] += timedelta(hours=4)
    tick()
    assert any(
        c.args[0] == 88 and "После паузы" in c.args[1] for c in sent.call_args_list
    )
    with sessions() as session:
        assert session.get(db.Scenario, waiting["id"]).active


def test_new_default_does_not_pause_keyword_or_cancel_its_timer(timer):
    client, sessions, _, _, _ = timer
    first = install(client, graph(keywords=None))
    data = wait_graph()
    data["entry"] = {"mode": "keywords", "keywords": "ждать курс", "match": "contains"}
    keyword = install(client, data)
    client.post("/vk/callback", json=event("ждать курс"))
    install(client, graph("Новый обычный", None))
    with sessions() as session:
        assert not session.get(db.Scenario, first["id"]).active
        assert session.get(db.Scenario, keyword["id"]).active
        assert session.query(db.ScenarioWait).one().status == "pending"


def test_same_keywords_are_isolated_between_projects(two_projects):
    env, first, second = two_projects
    for project, title in ((first, "Группа А"), (second, "Группа Б")):
        row = install(
            env.client, graph(title), route=prefix(project) + "/api/scenarios"
        )
        response = callback(env.client, project, message(project, text="хочу курс"))
        assert response.status_code == 200, response.text
        with session_for(project) as session:
            assert session.get(db.Conversation, 77).scenario_id == row["id"]
            assert (
                session.get(db.Scenario, row["id"]).published["nodes"][1]["text"]
                == title
            )
    sent = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert {c["params"]["message"] for c in sent} == {"Группа А", "Группа Б"}
    assert {c["params"]["access_token"] for c in sent} == {first.token, second.token}
