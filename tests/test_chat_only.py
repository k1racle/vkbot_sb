"""A comment can invite to an ordinary dialog without creating a gift."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from test_comments import campaign
from test_comments import comments as comments
from test_comments import event as comment
from test_gifts import invitations as invitations
from test_scenarios import create, db, dialog, login, main, publish, vk_api
from test_scenarios import event as message
from test_scenarios import setup as setup


def chat_fields(**kwargs):
    return {
        "title": "Диалог за комментарий",
        "post_id": "",
        "delivery_mode": "chat_only",
        "enabled": "1",
        "one_promo_per_user": "1",
        **kwargs,
    }


def scenario(client, campaign_id=None):
    flow = create(client)
    nodes = [
        {"id": "start", "type": "start", "next": "answer"},
        {"id": "answer", "type": "end", "text": "Чем помочь, {first_name}?"},
    ]
    if campaign_id:
        nodes[1] = {
            "id": "answer",
            "type": "promo",
            "campaign_id": campaign_id,
            "next": "end",
        }
        nodes.append({"id": "end", "type": "end"})
    flow = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={
            "title": "Общение",
            "revision": flow["revision"],
            "graph": {"nodes": nodes},
        },
    ).json()
    return flow


def test_save_without_promo_fields_and_render_mode(invitations):
    client, sessions, _, _ = invitations
    login(client)
    result = client.post("/admin/campaigns", data=chat_fields())
    assert "campaign_saved=1" in str(result.url)
    assert 'value="chat_only" selected' in result.text
    assert 'id="campaign-promo-fields" hidden disabled' in result.text
    with sessions() as session:
        row = session.query(db.Campaign).one()
        assert row.delivery_mode == "chat_only"
        assert row.promo_code == row.promo_message == row.shop_url == ""
        assert all("{chat_url}" in text for text in row.public_reply_variants)
        assert all("подар" not in text.casefold() for text in row.public_reply_variants)
    for mode in ("direct", "chat_invite"):
        response = client.post("/admin/campaigns", data=chat_fields(delivery_mode=mode))
        assert "campaign_error=promo" in str(response.url)


@pytest.mark.parametrize("variants", [["Без ссылки"], [" "], ["{chat_url}"] * 11])
def test_chat_only_validates_invitation(invitations, variants):
    client, sessions, _, _ = invitations
    login(client)
    result = client.post(
        "/admin/campaigns", data=chat_fields(public_reply_variants=variants)
    )
    assert "campaign_error=invitation" in str(result.url)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 0


@pytest.mark.parametrize(
    "payload,text",
    [
        (None, "Начать"),
        ({"command": "start"}, "Начать диалог"),
        (None, "Помогите выбрать"),
    ],
)
def test_comment_then_message_runs_scenario_without_gift(
    invitations, monkeypatch, payload, text
):
    client, sessions, replies, sent = invitations
    campaign(sessions, delivery_mode="chat_only")
    publish(client, scenario(client))
    member = AsyncMock(return_value=False)
    monkeypatch.setattr(vk_api, "is_group_member", member)
    monkeypatch.setattr(main, "is_group_member", member)
    assert client.post("/vk/callback", json=comment()).text == "ok"
    assert replies.call_count == 1
    assert "https://vk.me/club123" in replies.call_args.args[1]
    assert "подар" not in replies.call_args.args[1].casefold()
    sent.assert_not_called()  # A comment alone does not start a private scenario.
    event = message(text, payload=payload)
    client.post("/vk/callback", json=event)
    client.post("/vk/callback", json=event)
    assert sent.call_count == 1 and sent.call_args.args == (77, "Чем помочь, Анна?")
    member.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0
        assert session.query(db.PromoDelivery).count() == 0
        assert session.query(db.ProcessedComment).one().status == "chat_invited"


def test_protection_survives_restart_and_is_per_campaign(invitations):
    client, sessions, replies, _ = invitations
    campaign(sessions, delivery_mode="chat_only")
    client.post("/vk/callback", json=comment())
    db.init_db()
    for event in (comment(), comment(number=2), comment(object_id=595, number=3)):
        client.post("/vk/callback", json=event)
    assert replies.call_count == 1
    campaign(sessions, post_id=596, delivery_mode="chat_only")
    client.post("/vk/callback", json=comment(object_id=596))
    client.post("/vk/callback", json=comment(user=88, number=88))
    assert replies.call_count == 3
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0


def test_repeatable_still_deduplicates_vk_event(invitations):
    client, sessions, replies, _ = invitations
    campaign(sessions, delivery_mode="chat_only", one_promo_per_user=False)
    for event in (comment(), comment(), comment(number=2)):
        client.post("/vk/callback", json=event)
    assert replies.call_count == 2
    assert len({call.kwargs["guid"] for call in replies.call_args_list}) == 2


def test_filters_and_disabled_campaign(invitations):
    client, sessions, replies, _ = invitations
    ident = campaign(
        sessions,
        delivery_mode="chat_only",
        plus_words="подбор",
        stop_words="спам",
        min_comment_length=6,
    )
    for n, text in enumerate(["hi", "всё красиво", "подбор спам", "нужен подбор"]):
        client.post("/vk/callback", json=comment(content=text, number=n + 1))
    assert replies.call_count == 1
    with sessions() as session:
        assert [
            row.status
            for row in session.query(db.ProcessedComment).order_by(
                db.ProcessedComment.id
            )
        ] == [
            "too_short",
            "plus_word_missing",
            "stop_word",
            "chat_invited",
        ]
        session.get(db.Campaign, ident).enabled = False
        session.commit()
    client.post("/vk/callback", json=comment(content="нужен подбор", number=5, user=88))
    assert replies.call_count == 1


def test_test_mode_and_chat_disabled(invitations):
    client, sessions, replies, sent = invitations
    campaign(sessions, delivery_mode="chat_only")
    with sessions() as session:
        db.save_settings(
            session,
            {
                "test_mode": "true",
                "test_trigger_phrase": "проверка",
                "chat_enabled": "false",
            },
        )
    client.post("/vk/callback", json=comment())
    replies.assert_not_called()
    client.post("/vk/callback", json=comment(content="проверка", number=2))
    assert replies.call_count == 1
    client.post("/vk/callback", json=message("Начать", payload={"command": "start"}))
    sent.assert_not_called()


def test_switch_cancels_only_own_pending_gifts_preserves_promo_and_file(invitations):
    client, sessions, _, sent = invitations
    ident = campaign(
        sessions,
        delivery_mode="chat_invite",
        attachment_path="saved-file",
        attachment_name="photo.png",
    )
    client.post("/vk/callback", json=comment())
    with sessions() as session:
        session.add(
            db.PendingGift(
                id="other",
                user_id=88,
                campaign_id=999,
                event_key="other",
                active_key="88:999",
            )
        )
        session.commit()
    flow = scenario(client)
    publish(client, flow)
    response = client.post("/admin/campaigns", data=chat_fields(campaign_id=str(ident)))
    assert "campaign_saved=1" in str(response.url)
    with sessions() as session:
        own = session.query(db.PendingGift).filter_by(campaign_id=ident).one()
        assert (
            own.status == "cancelled"
            and own.active_key is None
            and not own.awaiting_subscription
        )
        assert session.get(db.PendingGift, "other").status == "pending"
        row = session.get(db.Campaign, ident)
        assert row.promo_code == "ALL" and row.attachment_path == "saved-file"
        assert session.query(db.ProcessedComment).one().status == "gift_cancelled"
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_args.args == (77, "Чем помочь, Анна?")
    with sessions() as session:
        assert session.query(db.PromoDelivery).count() == 0


def test_test_send_is_invitation_not_promo_or_comment(invitations, monkeypatch):
    client, sessions, replies, _ = invitations
    ident = campaign(
        sessions,
        delivery_mode="chat_only",
        public_reply_variants=["Поможем: {chat_url}"],
    )
    login(client)
    response = client.post(
        "/admin/test-send", data={"campaign_id": ident, "user_id": "77"}
    )
    assert "test_sent=1" in str(response.url)
    assert main.send_message.call_args.args == (77, "Поможем: https://vk.me/club123")
    button = main.send_message.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]
    assert json.loads(button["payload"]) == {"command": "start"}
    replies.assert_not_called()
    with sessions() as session:
        assert (
            session.query(db.PendingGift).count()
            == session.query(db.PromoDelivery).count()
            == 0
        )
        assert session.query(db.ProcessedComment).count() == 0


@pytest.mark.parametrize(
    "public,allowed,expected",
    [
        (True, False, "chat_invited"),
        (False, True, "chat_invited_dm"),
        (False, False, "chat_invite_unavailable"),
    ],
)
def test_video_public_and_permission_limited_fallback(
    invitations, monkeypatch, public, allowed, expected
):
    client, sessions, replies, sent = invitations
    campaign(sessions, delivery_mode="chat_only")
    video_reply = AsyncMock()
    monkeypatch.setattr(vk_api, "video_reply_available", lambda: public)
    monkeypatch.setattr(vk_api, "reply_to_video_comment", video_reply)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=allowed))
    client.post("/vk/callback", json=comment(source="video"))
    replies.assert_not_called()
    assert video_reply.call_count == int(public)
    assert sent.call_count == int(not public and allowed)
    if not public and allowed:
        action = sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]
        assert "подар" not in action["label"].casefold()
    with sessions() as session:
        assert (
            session.query(db.PendingGift).count()
            == session.query(db.PromoDelivery).count()
            == 0
        )
        assert session.query(db.ProcessedComment).one().status == expected


def test_chat_only_cannot_be_used_as_promo_block(invitations):
    client, sessions, _, sent = invitations
    ident = campaign(sessions, delivery_mode="chat_only")
    flow = scenario(client, campaign_id=ident)
    result = client.post(
        f"/admin/api/scenarios/{flow['id']}/publish",
        json={"revision": flow["revision"]},
    )
    assert result.status_code == 422
    assert not client.get("/admin/api/scenarios").json()["campaigns"]
    with sessions() as session:
        port = dialog.LivePort(session, 77, "test", {})
        asyncio.run(port.promo(ident, {}))
        assert session.query(db.PromoDelivery).count() == 0
    assert sent.call_args.args[1] == "Эта акция сейчас недоступна."
