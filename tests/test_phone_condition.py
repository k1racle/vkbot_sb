"""Phone presence is project-local CRM data, not an arbitrary flow variable."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

# These fixtures configure isolated databases and dummy credentials before app imports.
from test_scenarios import setup as setup, create, event
from test_flow_modules import graph, install
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    callback,
    message,
    session_for,
)
from app import db, vk_api
from app.flows import validate_graph


def phone_graph(mode="provided"):
    return graph(
        {
            "id": "check",
            "type": "phone_condition",
            "phone_check_mode": mode,
            "yes": "yes",
            "no": "no",
        },
        {"id": "yes", "type": "end", "text": "Телефон есть"},
        {"id": "no", "type": "end", "text": "Телефона нет"},
    )


def collect_graph(kind="phone", *, subflow=False):
    data = phone_graph()
    nodes = data["nodes"][1:]
    contact = {
        "id": "collect",
        "type": "contact",
        "contact_type": kind,
        "variable": "my_contact",
        "text": "Оставьте контакт",
        "next": "back" if subflow else "check",
    }
    if subflow:
        nodes = [
            {
                "id": "call",
                "type": "call_subflow",
                "subflow_id": "sub",
                "next": "check",
            },
            *nodes,
            {"id": "sub", "type": "subflow", "next": "collect"},
            contact,
            {"id": "back", "type": "return"},
        ]
    else:
        nodes = [contact, *nodes]
    return graph(*nodes)


def test_phone_block_validation():
    data = phone_graph()
    assert not validate_graph(data)
    for path in ("yes", "no"):
        broken = phone_graph()
        broken["nodes"][1][path] = ""
        assert validate_graph(broken)
    data["nodes"][1]["yes"] = "check"
    assert any("цикл" in issue for issue in validate_graph(data))
    with pytest.raises(ValidationError):
        phone_graph("anything")
    assert (
        graph({"id": "p", "type": "phone_condition"})["nodes"][1]["phone_check_mode"]
        == "provided"
    )


@pytest.mark.parametrize(
    "mode,source,number,expected",
    [
        ("provided", "dialog", "+79991234567", True),
        ("provided", "vk", "+79991234567", False),
        ("provided", "", "+79991234567", False),
        ("any", "dialog", "+79991234567", True),
        ("any", "vk", "+79991234567", True),
        ("any", "", "+79991234567", True),
        ("any", "dialog", "", False),
        ("provided", "dialog", "   ", False),
        ("provided", "dialog", "123", False),
        ("any", "dialog", "не оставил", False),
        ("provided", "dialog", "+7 (999) 123-45-67", True),
    ],
)
def test_live_phone_checks_stored_number_and_source(
    setup, monkeypatch, mode, source, number, expected
):
    client, sessions, sent = setup
    install(client, phone_graph(mode))
    api = AsyncMock(side_effect=AssertionError("Phone presence must not query VK"))
    monkeypatch.setattr(vk_api, "call", api)
    with sessions() as session:
        session.add(
            db.Client(user_id=77, phone=number, phone_source=source, unsubscribed=True)
        )
        session.commit()
    response = client.post("/vk/callback", json=event())
    assert response.status_code == 200
    assert sent.call_args.args[1] == ("Телефон есть" if expected else "Телефона нет")
    with sessions() as session:
        saved = session.get(db.Client, 77)
        assert saved.phone == number and saved.phone_source == source
        assert saved.unsubscribed  # This condition grants no marketing consent.
    api.assert_not_called()


def test_missing_card_and_phone_variable_do_not_satisfy_condition(setup):
    client, _, sent = setup
    data = phone_graph()
    data = graph(
        {
            "id": "fake",
            "type": "set_variable",
            "variable": "phone",
            "value": "+79991234567",
            "next": "check",
        },
        *data["nodes"][1:],
    )
    install(client, data)
    client.post("/vk/callback", json=event("+79991234567"))
    assert sent.call_args.args[1] == "Телефона нет"


@pytest.mark.parametrize(
    "kind,answer,expected",
    [
        ("phone", "+7 (999) 123-45-67", True),
        ("phone", "Пропустить", False),
        ("email", "test@example.com", False),
    ],
)
def test_collected_contact_immediately_affects_check(setup, kind, answer, expected):
    client, sessions, sent = setup
    install(client, collect_graph(kind))
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event(answer, 2))
    assert sent.call_args.args[1] == ("Телефон есть" if expected else "Телефона нет")
    with sessions() as session:
        assert bool(session.get(db.Client, 77).phone) is expected


def test_invalid_contact_still_waits_and_menu_preserves_phone(setup):
    client, sessions, sent = setup
    install(client, collect_graph())
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("123", 2))
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "collect"
        assert not session.get(db.Client, 77).phone
    client.post("/vk/callback", json=event("89991234567", 3))
    assert sent.call_args.args[1] == "Телефон есть"
    client.post("/vk/callback", json=event("Меню", 4))
    client.post("/vk/callback", json=event("Пропустить", 5))
    assert sent.call_args.args[1] == "Телефон есть"
    with sessions() as session:
        assert "my_contact" not in session.get(db.Conversation, 77).variables
        assert session.get(db.Client, 77).phone == "+79991234567"


def test_subflow_phone_survives_return_without_exported_variables(setup):
    client, sessions, sent = setup
    data = collect_graph(subflow=True)
    assert not validate_graph(data)
    install(client, data)
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("89991234567", 2))
    assert sent.call_args.args[1] == "Телефон есть"
    with sessions() as session:
        assert "my_contact" not in session.get(db.Conversation, 77).variables


@pytest.mark.parametrize(
    "mode,status,expected",
    [
        ("provided", "missing", False),
        ("provided", "provided", True),
        ("provided", "profile", False),
        ("any", "missing", False),
        ("any", "provided", True),
        ("any", "profile", True),
    ],
)
def test_preview_phone_presets_without_real_contacts(setup, mode, status, expected):
    client, sessions, sent = setup
    create(client)
    response = client.post(
        "/admin/api/preview", json={"graph": phone_graph(mode), "phone_status": status}
    )
    assert response.status_code == 200, response.text
    assert response.json()["messages"][0]["text"] == (
        "Телефон есть" if expected else "Телефона нет"
    )
    with sessions() as session:
        assert session.query(db.Client).count() == 0
    sent.assert_not_called()


@pytest.mark.parametrize(
    "kind,answer,status,expected",
    [
        ("phone", "89991234567", "missing", True),
        ("phone", "Пропустить", "missing", False),
        ("phone", "Пропустить", "provided", True),
        ("email", "test@example.com", "missing", False),
    ],
)
def test_preview_collects_only_virtual_phone(setup, kind, answer, status, expected):
    client, sessions, sent = setup
    create(client)
    data = collect_graph(kind, subflow=True)
    first = client.post(
        "/admin/api/preview", json={"graph": data, "phone_status": status}
    ).json()
    result = client.post(
        "/admin/api/preview",
        json={"graph": data, "state": first["state"], "text": answer},
    ).json()
    assert result["messages"][0]["text"] == (
        "Телефон есть" if expected else "Телефона нет"
    )
    assert result["state"]["phone_status"] == ("provided" if expected else "missing")
    assert "my_contact" not in result["state"]["variables"]
    with sessions() as session:
        assert session.query(db.Client).count() == 0
    sent.assert_not_called()


def test_preview_rejects_invalid_phone_status(setup):
    client, _, _ = setup
    create(client)
    result = client.post(
        "/admin/api/preview", json={"graph": phone_graph(), "phone_status": "invalid"}
    )
    assert result.status_code == 422


def test_same_user_phone_is_not_shared_between_projects(two_projects):
    env, first, second = two_projects
    for project in (first, second):
        route = prefix(project) + "/api/scenarios"
        flow = env.client.post(route).json()
        flow = env.client.put(
            f"{route}/{flow['id']}",
            json={
                "title": "Проверка телефона",
                "graph": phone_graph(),
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
            if project.id == first.id:
                session.add(
                    db.Client(user_id=77, phone="+79991234567", phone_source="dialog")
                )
                session.commit()
        assert callback(env.client, project, message(project)).status_code == 200
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert [(c["project_id"], c["params"]["message"]) for c in sends] == [
        (first.id, "Телефон есть"),
        (second.id, "Телефона нет"),
    ]
