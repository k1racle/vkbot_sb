"""New blocks share one interpreter between VK and the offline simulator."""

import asyncio
import copy
import hashlib
from unittest.mock import AsyncMock

import pytest

from test_scenarios import setup as setup, create, event, publish
from app import clients, db, vk_api
from app.flows import (
    Graph,
    advance,
    compare_variable,
    normalize_contact,
    validate_graph,
)


def graph(*nodes):
    return Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "next": nodes[0]["id"]},
                *nodes,
            ]
        }
    ).model_dump()


def contact_graph():
    return graph(
        {
            "id": "phone",
            "type": "contact",
            "contact_type": "phone",
            "variable": "phone",
            "text": "Ваш телефон?",
            "next": "email",
        },
        {
            "id": "email",
            "type": "contact",
            "contact_type": "email",
            "variable": "email",
            "text": "Ваш email?",
            "next": "save",
        },
        {
            "id": "save",
            "type": "set_variable",
            "variable": "request",
            "value": "Связь: {email}",
            "next": "check",
        },
        {
            "id": "check",
            "type": "variable_condition",
            "variable": "phone",
            "comparison": "not_empty",
            "yes": "random",
            "no": "end",
        },
        {
            "id": "random",
            "type": "random",
            "variants": ["Спасибо, {first_name}!", "Рады помочь, {first_name}!"],
            "next": "end",
        },
        {"id": "end", "type": "end", "text": "Готово"},
    )


class Port:
    def __init__(self, key="event-1"):
        self.key, self.messages, self.contacts = key, [], []

    def nonce(self, step):
        return hashlib.sha256(f"{self.key}:{step}".encode()).hexdigest()

    async def emit(self, text, **kwargs):
        self.messages.append((text, kwargs))

    async def save_contact(self, kind, value):
        self.contacts.append((kind, value))

    async def cancel_contact_wait(self, state, reason):
        state.pop("waiting", None)


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("phone", "+7 (999) 123-45-67", "+79991234567"),
        ("phone", "8 999 1234567", "+79991234567"),
        ("phone", "+44 20 7123 4567", "+442071234567"),
        ("phone", "123", None),
        ("phone", "телефон 89991234567", None),
        ("phone", "+7+9991234567", None),
        ("phone", "1" * 16, None),
        ("email", " Anna+shop@EXAMPLE.COM ", "Anna+shop@example.com"),
        ("email", "name@пример.рф", "name@xn--e1afmkfd.xn--p1ai"),
        ("email", "name@localhost", None),
        ("email", "a b@example.com", None),
        ("email", "a..b@example.com", None),
        ("email", "a@-example.com", None),
        ("email", "a@example.com\nsecond@example.com", None),
    ],
)
def test_contact_format(kind, value, expected):
    assert normalize_contact(value, kind) == expected


@pytest.mark.parametrize(
    "comparison,value,expected,target",
    [
        ("equals", " ДОСТАВКА ", "доставка", True),
        ("not_equals", "самовывоз", "доставка", True),
        ("contains", "Нужна доставка", "ДОСТАВКА", True),
        ("empty", None, "", True),
        ("not_empty", 0, "", True),
        ("not_equals", None, "доставка", False),
        ("gt", "1,5", "1.4", True),
        ("gte", "0", "0", True),
        ("lte", "1000", "999", False),
        ("lt", "не знаю", "50", False),
        ("gt", "NaN", "0", False),
        ("gt", "Infinity", "0", False),
    ],
)
def test_compare_answers(comparison, value, expected, target):
    node = {"variable": "answer", "comparison": comparison, "value": expected}
    assert compare_variable(node, {"answer": value}) is target


def test_new_graph_validation_and_wait_boundaries():
    valid = contact_graph()
    assert validate_graph(valid) == []
    for key in ("first_name", "last_message", "_nonce", "bad-name"):
        invalid = copy.deepcopy(valid)
        invalid["nodes"][3]["variable"] = key
        assert validate_graph(invalid)
    for variants in (["Only one"], ["", "Two"]):
        invalid = copy.deepcopy(valid)
        invalid["nodes"][5]["variants"] = variants
        assert validate_graph(invalid)
    bad_number = copy.deepcopy(valid)
    bad_number["nodes"][4].update(comparison="gt", value="NaN")
    assert validate_graph(bad_number)
    cycle = graph(
        {
            "id": "set",
            "type": "set_variable",
            "variable": "choice",
            "value": "yes",
            "next": "test",
        },
        {
            "id": "test",
            "type": "variable_condition",
            "variable": "choice",
            "comparison": "equals",
            "value": "yes",
            "yes": "set",
            "no": "end",
        },
        {"id": "end", "type": "end"},
    )
    assert any("цикл" in text for text in validate_graph(cycle))
    # A contact block waits for a new message, so a retry loop is bounded.
    contact_loop = graph(
        {
            "id": "phone",
            "type": "contact",
            "variable": "phone",
            "text": "Телефон?",
            "next": "phone",
        }
    )
    assert validate_graph(contact_loop) == []


def test_contacts_wait_validate_skip_and_do_not_accept_old_buttons():
    async def run():
        g, state, port = (
            contact_graph(),
            {"version": 1, "variables": {"first_name": "Анна"}},
            Port(),
        )
        await advance(g, state, "привет", {}, port)
        assert state["node_id"] == "phone"
        assert "Пропустить" in port.messages[-1][0]
        await advance(g, state, "не номер", {}, port)
        assert state["node_id"] == "phone" and not port.contacts
        await advance(g, state, "+79991234567", {"node": "old", "button": 0}, port)
        assert state["node_id"] == "phone" and not port.contacts
        await advance(g, state, "8 999 1234567", {}, port)
        assert (
            state["node_id"] == "email"
            and state["variables"]["phone"] == "+79991234567"
        )
        await advance(g, state, "bad@", {}, port)
        assert state["node_id"] == "email"
        await advance(g, state, "Anna@example.com", {}, port)
        assert state["node_id"] == ""
        assert state["variables"]["request"] == "Связь: Anna@example.com"
        assert port.contacts == [
            ("phone", "+79991234567"),
            ("email", "Anna@example.com"),
        ]
        assert port.messages[-1][0] == "Готово"
        other, skipped = Port(), {"version": 1}
        await advance(g, skipped, "привет", {}, other)
        await advance(g, skipped, "пропустить", {}, other)
        await advance(g, skipped, "Пропустить", {}, other)
        assert skipped["node_id"] == "" and not other.contacts
        assert "phone" not in skipped["variables"]
        assert len(other.messages) == 3  # No random reply on the NO branch.

    asyncio.run(run())


def test_contact_prompt_with_skip_hint_stays_within_message_limit():
    async def run():
        g = contact_graph()
        g["nodes"][1]["text"] = "{answer}"
        state, port = {"version": 1, "variables": {"answer": "x" * 5000}}, Port()
        await advance(g, state, "привет", {}, port)
        assert len(port.messages[-1][0]) <= 4000
        assert "Пропустить" in port.messages[-1][0]

    asyncio.run(run())


def test_required_contact_and_custom_error_do_not_advance():
    async def run():
        g = contact_graph()
        g["nodes"][1].update(allow_skip=False, error_text="Нужен номер для связи")
        state, port = {"version": 1}, Port()
        await advance(g, state, "привет", {}, port)
        assert "Пропустить" not in port.messages[-1][0]
        await advance(g, state, "пропустить", {}, port)
        assert port.messages[-1][0] == "Нужен номер для связи"
        assert state["node_id"] == "phone"

    asyncio.run(run())


def test_random_variant_stable_on_retry_and_varied_between_events():
    async def run():
        g = graph(
            {
                "id": "random",
                "type": "random",
                "variants": ["А {first_name}", "Б {first_name}"],
                "next": "end",
            },
            {"id": "end", "type": "end"},
        )

        async def reply(key):
            port = Port(key)
            await advance(
                g,
                {"version": 1, "variables": {"first_name": "Анна"}},
                "привет",
                {},
                port,
            )
            return port.messages

        assert await reply("same") == await reply("same")
        assert len({(await reply(str(i)))[0][0] for i in range(30)}) == 2

    asyncio.run(run())


def install(client, g):
    flow = create(client)
    response = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={"title": "Новые блоки", "graph": g, "revision": flow["revision"]},
    )
    assert response.status_code == 200, response.text
    return publish(client, response.json())


def test_live_contacts_persist_in_client_and_answers_without_marketing_opt_in(setup):
    client, sessions, sent = setup
    install(client, contact_graph())
    client.post("/vk/callback", json=event())
    with sessions() as session:
        customer = session.get(db.Client, 77)
        customer.unsubscribed = True
        session.commit()
    client.post("/vk/callback", json=event("bad", 2))
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "phone"
        assert not session.get(db.Client, 77).phone
    client.post("/vk/callback", json=event("8 999 1234567", 3))
    client.post("/vk/callback", json=event("Anna@example.com", 4))
    before = sent.call_count
    client.post("/vk/callback", json=event("Anna@example.com", 4))
    assert sent.call_count == before
    with sessions() as session:
        assert session.get(db.Client, 77).phone == "+79991234567"
        assert session.get(db.Client, 77).phone_source == "dialog"
        clients.save_profile(session, {"id": 77, "mobile_phone": "+79990000000"})
        assert session.get(db.Client, 77).phone == "+79991234567"
        assert session.get(db.Client, 77).unsubscribed
        conversation = session.get(db.Conversation, 77)
        assert conversation.variables["email"] == "Anna@example.com"
        assert conversation.variables["request"] == "Связь: Anna@example.com"
        assert not conversation.node_id


def test_preview_all_modules_does_not_write_contacts_or_call_vk(setup):
    client, sessions, sent = setup
    create(client)
    g = contact_graph()
    state = {}
    for answer in ("привет", "+79991234567", "a@example.com"):
        response = client.post(
            "/admin/api/preview", json={"graph": g, "state": state, "text": answer}
        )
        assert response.status_code == 200, response.text
        state = response.json()["state"]
    assert state["variables"]["request"] == "Связь: a@example.com"
    assert state["node_id"] == ""
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.Client).count() == 0


def test_random_vk_retry_uses_same_text_and_random_id(setup, monkeypatch):
    client, _, _ = setup
    g = graph(
        {"id": "r", "type": "random", "variants": ["Первый", "Второй"], "next": "end"},
        {"id": "end", "type": "end"},
    )
    install(client, g)
    mock = AsyncMock(side_effect=[vk_api.VkApiError("temporary"), None])
    monkeypatch.setattr(vk_api, "send_message", mock)
    with pytest.raises(vk_api.VkApiError):
        client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event())
    assert mock.call_count == 2
    assert mock.call_args_list[0] == mock.call_args_list[1]
