import asyncio
from datetime import datetime

import pytest
from pydantic import ValidationError

from test_flow_modules import Port, graph, install
from test_scenarios import setup as setup, login, event
from app import db
from app.flows import Graph, advance, extract_contacts, normalize_contact


def multi_graph(requirement="any"):
    return graph(
        {
            "id": "contact",
            "type": "contact",
            "variable": "contact",
            "text": "Оставьте контакты",
            "contact_types": ["phone", "email", "messenger"],
            "contact_requirement": requirement,
        }
    )


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("+7 (999) 123-45-67", {"phone": "+79991234567"}),
        ("anna@example.com", {"email": "anna@example.com"}),
        ("https://t.me/anna", {"messenger": "https://t.me/anna"}),
        (
            "Телефон: +7 (999) 123-45-67, почта: anna@example.com; https://t.me/anna",
            {
                "phone": "+79991234567",
                "email": "anna@example.com",
                "messenger": "https://t.me/anna",
            },
        ),
        ("https://wa.me/79991234567", {"messenger": "https://wa.me/79991234567"}),
        ("не знаю", {}),
    ],
)
def test_extract_and_any_contact(reply, expected):
    async def run():
        g, state, port = multi_graph(), {"version": 1}, Port()
        assert extract_contacts(reply, g["nodes"][1]) == expected
        await advance(g, state, "start", {}, port)
        await advance(g, state, reply, {}, port)
        assert dict(port.contacts) == expected
        assert state["node_id"] == ("" if expected else "contact")
        for kind, value in expected.items():
            assert state["variables"][f"contact_{kind}"] == value

    asyncio.run(run())


def test_all_contacts_across_messages_and_restart():
    async def run():
        g, state, port = multi_graph("all"), {"version": 1}, Port()
        await advance(g, state, "start", {}, port)
        await advance(g, state, "anna@example.com", {}, port)
        assert state["node_id"] == "contact" and not port.contacts
        await advance(g, state, "start", {}, port, restart=True)
        assert "_contact_values" not in state["variables"]
        await advance(g, state, "+79991234567", {}, port)
        await advance(g, state, "https://t.me/anna", {}, port)
        assert state["node_id"] == "contact"
        await advance(g, state, "anna@example.com", {}, port)
        assert state["node_id"] == ""
        assert len(port.contacts) == 3
        assert (
            state["variables"]["contact"]
            == "+79991234567; anna@example.com; https://t.me/anna"
        )
        assert "_contact_values" not in state["variables"]

    asyncio.run(run())


def test_skip_partial_contacts():
    async def run():
        g, state, port = multi_graph("all"), {"version": 1}, Port()
        await advance(g, state, "start", {}, port)
        await advance(g, state, "anna@example.com", {}, port)
        await advance(g, state, "Пропустить", {}, port)
        assert state["node_id"] == "" and not port.contacts
        assert "_contact_values" not in state["variables"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://t.me.evil.test/anna",
        "https://t.me/",
        "https://user:pass@t.me/anna",
    ],
)
def test_invalid_messenger(url):
    assert normalize_contact(url, "messenger") is None


def test_empty_contact_selection_rejected():
    g = multi_graph()
    g["nodes"][1]["contact_types"] = []
    with pytest.raises(ValidationError):
        Graph.model_validate(g)


def test_multi_contacts_persist_through_callback_and_preview(setup):
    client, sessions, _ = setup
    g = multi_graph("all")
    install(client, g)
    answers = ["привет", "anna@example.com", "+79991234567", "https://t.me/anna"]
    preview = {}
    for number, answer in enumerate(answers, 1):
        assert (
            client.post("/vk/callback", json=event(answer, number)).status_code == 200
        )
        response = client.post(
            "/admin/api/preview", json={"graph": g, "state": preview, "text": answer}
        )
        assert response.status_code == 200, response.text
        preview = response.json()["state"]
    with sessions() as session:
        conversation = session.get(db.Conversation, 77)
        assert not conversation.node_id
        assert session.get(db.Client, 77).phone == "+79991234567"
        for key in ("contact", "contact_phone", "contact_email", "contact_messenger"):
            assert conversation.variables[key] == preview["variables"][key]


def test_statistics_show_interaction_time_in_moscow(setup):
    client, sessions, _ = setup
    when = datetime(2026, 9, 29, 22, 15, 30)
    with sessions() as session:
        session.add(
            db.DialogEvent(
                event_key="stats-dialog",
                user_id=77,
                text="Контакт",
                status="done",
                created_at=when,
            )
        )
        session.add(
            db.ProcessedComment(
                event_key="stats-comment",
                owner_id=-123,
                comment_id=101,
                post_id=12,
                user_id=77,
                created_at=when,
            )
        )
        session.commit()
    login(client)
    response = client.get("/admin?section=stats")
    assert response.status_code == 200
    assert response.text.count("30.09.2026 01:15:30") == 2
    assert "Взаимодействия в сценариях и диалогах" in response.text
    assert "Контакт" in response.text
