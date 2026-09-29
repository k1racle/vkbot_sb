"""Gift commands must not leak into projects configured for ordinary chats."""

import json
from unittest.mock import AsyncMock

import pytest

from test_scenarios import setup as setup, event, db, vk_api
from test_comments import campaign
from test_scenario_triggers import graph, install
from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    prefix,
    session_for,
    message,
    callback,
)


@pytest.mark.parametrize("kind", ["none", "chat_only", "disabled", "deleted"])
@pytest.mark.parametrize(
    "text,payload",
    [
        ("Подарок", None),
        ("/gift", None),
        ("Получить подарок", {"action": "claim_gift"}),
    ],
)
def test_no_gift_campaign_uses_normal_scenario(setup, monkeypatch, kind, text, payload):
    client, sessions, sent = setup
    if kind != "none":
        campaign(
            sessions,
            delivery_mode="chat_only" if kind == "chat_only" else "chat_invite",
            enabled=kind != "disabled",
            is_deleted=kind == "deleted",
        )
    install(client, graph(title="Обычный диалог", keywords=None))
    member = AsyncMock(return_value=False)
    monkeypatch.setattr(vk_api, "is_group_member", member)
    body = event(text, payload=payload)
    for _ in range(2):
        assert client.post("/vk/callback", json=body).text == "ok"
    assert sent.call_count == 1
    assert sent.call_args.args == (77, "Обычный диалог")
    member.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0
        assert session.query(db.PromoDelivery).count() == 0
        assert session.query(db.DialogEvent).one().kind != "gift"
        assert session.get(db.Conversation, 77).node_id == "question"


@pytest.mark.parametrize("payload", [None, {"action": "claim_gift"}])
def test_gift_word_can_trigger_chat_only_keyword_scenario(setup, payload):
    client, sessions, sent = setup
    campaign(sessions, delivery_mode="chat_only")
    install(client, graph(title="Расскажу про курс", keywords="подарок"))
    client.post("/vk/callback", json=event("Подарок", payload=payload))
    assert sent.call_count == 1
    assert sent.call_args.args[1] == "Расскажу про курс"


@pytest.mark.parametrize(
    "greeting,enabled", [("", True), ("Ваш вопрос?", True), ("Ваш вопрос?", False)]
)
def test_no_gift_context_respects_empty_fallback_and_chat_switch(
    setup, greeting, enabled
):
    client, sessions, sent = setup
    with sessions() as session:
        db.save_settings(
            session,
            {"chat_greeting": greeting, "chat_enabled": "true" if enabled else "false"},
        )
    client.post(
        "/vk/callback",
        json=event("Получить подарок", payload={"action": "claim_gift"}),
    )
    if greeting and enabled:
        assert sent.call_count == 1 and sent.call_args.args[1] == greeting
    else:
        sent.assert_not_called()


@pytest.mark.parametrize("mode", ["direct", "chat_invite"])
def test_active_gift_campaign_keeps_explicit_no_gift_reply(setup, mode):
    client, sessions, sent = setup
    campaign(sessions, delivery_mode=mode)
    install(client, graph(title="Обычный диалог", keywords=None))
    client.post("/vk/callback", json=event("Подарок"))
    assert sent.call_count == 1
    assert sent.call_args.args[1].startswith("Пока нет подарков к получению.")
    assert "меню" not in sent.call_args.args[1].casefold()


def test_gift_campaign_in_other_project_does_not_intercept_chat(two_projects):
    env, first, second = two_projects
    with session_for(first) as session:
        session.add(db.Campaign(title="Подарки", delivery_mode="chat_invite"))
        session.commit()
    with session_for(second) as session:
        session.add(db.Campaign(title="Курс", delivery_mode="chat_only"))
        session.commit()
    install(
        env.client,
        graph(title="Курс второй группы", keywords="подарок"),
        route=prefix(second) + "/api/scenarios",
    )
    for project in (first, second):
        body = message(project, text="Подарок")
        body["object"]["message"]["payload"] = json.dumps({"action": "claim_gift"})
        assert callback(env.client, project, body).text == "ok"
    sends = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert len(sends) == 2
    assert sends[0]["project_id"] == first.id
    assert sends[0]["params"]["message"].startswith("Пока нет подарков")
    assert sends[1]["project_id"] == second.id
    assert sends[1]["params"]["message"] == "Курс второй группы"
