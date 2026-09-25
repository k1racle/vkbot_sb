import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_comments import campaign, fields
from test_comments import comments as comments
from test_comments import event as comment
from test_scenarios import db, login, main, vk_api
from test_scenarios import event as message
from test_scenarios import setup as setup

from app import gifts
from app.comments import normalize_comment


@pytest.fixture
def invitations(comments, monkeypatch):
    client, sessions, _ = comments
    replies = AsyncMock()
    monkeypatch.setattr(vk_api, "reply_to_wall_comment", replies)
    return client, sessions, replies, vk_api.send_message


def invite(client, sessions, **options):
    ident = campaign(sessions, delivery_mode="chat_invite", **options)
    assert client.post("/vk/callback", json=comment()).text == "ok"
    return ident


def test_variants_admin_round_trip_and_old_mode_preserved(invitations):
    client, sessions, _, _ = invitations
    login(client)
    variants = ["Спасибо! {chat_url}", "Ваш подарок: {chat_url}"]
    result = client.post(
        "/admin/campaigns",
        data=fields(
            delivery_mode="chat_invite",
            public_reply_variants=variants,
        ),
    )
    assert "campaign_saved=1" in str(result.url)
    assert "Спасибо! {chat_url}" in result.text
    with sessions() as session:
        row = session.query(db.Campaign).one()
        assert row.delivery_mode == "chat_invite"
        assert row.public_reply_variants == variants
        ident = row.id
    client.post("/admin/campaigns", data=fields(campaign_id=str(ident)))
    with sessions() as session:
        assert session.get(db.Campaign, ident).delivery_mode == "direct"


@pytest.mark.parametrize(
    "variants",
    [
        ["Без ссылки"],
        [" "],
        ["{chat_url}"] * 11,
        ["{chat_url}" + "x" * 2001],
    ],
)
def test_bad_invitation_never_saved(invitations, variants):
    client, sessions, _, _ = invitations
    login(client)
    response = client.post(
        "/admin/campaigns",
        data=fields(
            delivery_mode="chat_invite",
            public_reply_variants=variants,
        ),
    )
    assert "campaign_error=invitation" in str(response.url)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 0


def test_queue_dedup_start_claim_and_delivery_history(invitations):
    client, sessions, replies, sent = invitations
    ident = invite(client, sessions)
    sent.assert_not_called()
    assert replies.call_count == 1
    assert "https://vk.me/club123" in replies.call_args.args[1]
    assert "ALL" not in replies.call_args.args[1]  # No public promo code.
    for data in (comment(), comment(number=2), comment(object_id=595, number=3)):
        client.post("/vk/callback", json=data)
    assert replies.call_count == 1
    with sessions() as session:
        gift = session.query(db.PendingGift).one()
        assert gift.status == "pending"
        assert gift.invitation_text == replies.call_args.args[1]
        assert gift.id == replies.call_args.kwargs["guid"]
    db.init_db()  # Simulate an upgrade/restart: no in-memory pending state.
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_count == 1
    assert sent.call_args.args == (77, "Код ALL")
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_count == 1
    client.post("/vk/callback", json=comment(number=4))
    assert replies.call_count == 1
    with sessions() as session:
        gift = session.query(db.PendingGift).one()
        assert gift.status == "sent" and gift.active_key is None
        assert session.query(db.PromoDelivery).count() == 1
        assert gifts.delivered(session, 77, session.get(db.Campaign, ident))
        assert session.query(db.ProcessedComment).filter_by(status="sent").count() == 1


def test_random_variants_are_selected_and_saved(invitations, monkeypatch):
    client, sessions, replies, _ = invitations
    variants = ["Раз: {chat_url}", "Два: {chat_url}"]
    campaign(sessions, delivery_mode="chat_invite", public_reply_variants=variants)
    selected = iter(variants)
    monkeypatch.setattr(gifts.secrets, "choice", lambda options: next(selected))
    for user in (77, 88):
        client.post("/vk/callback", json=comment(user=user, number=user))
    assert [call.args[1] for call in replies.call_args_list] == [
        "Раз: https://vk.me/club123",
        "Два: https://vk.me/club123",
    ]


def test_claim_does_not_require_chat_enabled_or_reset_manager(invitations):
    client, sessions, _, sent = invitations
    invite(client, sessions)
    with sessions() as session:
        session.add(
            db.Conversation(
                user_id=77,
                handoff=True,
                assigned_operator_id=99,
                node_id="question",
                variables={"answer": "ok"},
            )
        )
        db.save_settings(session, {"chat_enabled": "false"})
    client.post("/vk/callback", json=message("", payload={"command": "start"}))
    assert sent.call_args.args[1] == "Код ALL"
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.handoff and row.assigned_operator_id == 99
        assert row.node_id == "question" and row.variables == {"answer": "ok"}


def test_cannot_claim_another_user_or_group_gift(invitations):
    client, sessions, _, sent = invitations
    invite(client, sessions)
    with sessions() as session:
        gift_id = session.query(db.PendingGift).one().id
    client.post(
        "/vk/callback",
        json=message(
            "Подарок",
            user=88,
            payload={"action": "claim_gift", "gift_id": gift_id, "user_id": 77},
        ),
    )
    assert "Пока нет подарков" in sent.call_args.args[1]
    forged = message("Подарок", number=2)
    forged["group_id"] = 999
    client.post("/vk/callback", json=forged)
    assert sent.call_count == 1
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "pending"


def test_membership_rechecked_and_button_can_retry(invitations, monkeypatch):
    client, sessions, _, sent = invitations
    invite(client, sessions)
    monkeypatch.setattr(vk_api, "is_group_member", AsyncMock(return_value=False))
    client.post("/vk/callback", json=message("Подарок"))
    assert "подпишитесь" in sent.call_args.args[1]
    action = sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]
    assert action["label"] == "Проверить подписку"
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "pending"
    monkeypatch.setattr(vk_api, "is_group_member", AsyncMock(return_value=True))
    client.post(
        "/vk/callback",
        json=message("Проверить подписку", number=2, payload=action["payload"]),
    )
    assert sent.call_args.args[1] == "Код ALL"


@pytest.mark.parametrize(
    "change,status",
    [
        ("disable", "gift_unavailable"),
        ("delete", "gift_unavailable"),
        ("already_delivered", "already_sent"),
    ],
)
def test_recheck_campaign_and_other_delivery(invitations, change, status):
    client, sessions, _, sent = invitations
    ident = invite(client, sessions)
    with sessions() as session:
        row = session.get(db.Campaign, ident)
        if change == "delete":
            session.delete(row)
        elif change == "disable":
            row.enabled = False
        else:
            session.add(db.PromoDelivery(user_id=77, campaign_id=ident))
        session.commit()
    client.post("/vk/callback", json=message("Подарок"))
    assert sent.call_args.args[1] != "Код ALL"
    with sessions() as session:
        assert session.query(db.ProcessedComment).one().status == status
        assert session.query(db.PendingGift).one().status == "cancelled"


def test_public_reply_error_keeps_gift_and_does_not_flood(invitations):
    client, sessions, replies, sent = invitations
    replies.side_effect = vk_api.VkApiError("15: Access denied")
    invite(client, sessions)
    client.post("/vk/callback", json=comment(number=2))
    assert replies.call_count == 1
    with sessions() as session:
        assert (
            session.query(db.ProcessedComment).filter_by(status="failed").count() == 1
        )
    client.post("/vk/callback", json=message("Подарок"))
    assert sent.call_args.args[1] == "Код ALL"


def test_failed_dm_retries_same_gift_and_does_not_consume_next(invitations):
    client, sessions, _, sent = invitations
    ident = invite(client, sessions)
    sent.side_effect = vk_api.VkApiError("6: Too many requests")
    with pytest.raises(vk_api.VkApiError):
        client.post("/vk/callback", json=message("Подарок"))
    failed_id = sent.call_args.kwargs["random_id"]
    with sessions() as session:
        session.get(db.Campaign, ident).promo_code = "CHANGED"
        gift = session.query(db.PendingGift).one()
        assert gift.status == "pending" and gift.delivery_payload["text"] == "Код ALL"
        assert session.query(db.DialogEvent).one().gift_id == gift.id
        session.commit()
    sent.side_effect = None
    client.post("/vk/callback", json=message("Подарок", number=2))
    assert sent.call_args.kwargs["random_id"] == failed_id
    assert sent.call_args.args[1] == "Код ALL"
    campaign(sessions, post_id=888, delivery_mode="chat_invite")
    client.post("/vk/callback", json=comment(object_id=888, number=3))
    before = sent.call_count
    client.post("/vk/callback", json=message("Подарок"))  # Late retry of failed event.
    assert sent.call_count == before
    with sessions() as session:
        assert session.query(db.PendingGift).filter_by(status="pending").count() == 1


def test_multiple_gifts_next_button_and_repeatable_campaign(invitations):
    client, sessions, replies, sent = invitations
    invite(client, sessions, one_promo_per_user=False)
    campaign(sessions, post_id=888, delivery_mode="chat_invite")
    client.post("/vk/callback", json=comment(object_id=888, number=2))
    client.post("/vk/callback", json=message("Подарок"))
    action = sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]
    assert action["label"] == "Следующий подарок"
    client.post(
        "/vk/callback",
        json=message("Следующий подарок", number=2, payload=action["payload"]),
    )
    assert sent.call_count == 2
    client.post("/vk/callback", json=comment(number=3))
    assert replies.call_count == 3  # New entitlement only after previous gift claimed.


def test_media_is_uploaded_only_after_entering_chat(invitations, monkeypatch, tmp_path):
    client, sessions, _, sent = invitations
    upload = AsyncMock(return_value="photo-123_456")
    monkeypatch.setattr(vk_api, "upload_file_for_message", upload)
    invite(
        client,
        sessions,
        attachment_path=str(tmp_path / "image.png"),
        attachment_name="image.png",
        attachment_type="image/png",
    )
    upload.assert_not_called()
    client.post("/vk/callback", json=message("Подарок"))
    assert sent.call_args.kwargs["attachment"] == "photo-123_456"


def test_invite_filters_and_video_limitation(invitations, monkeypatch):
    client, sessions, replies, sent = invitations
    campaign(
        sessions, delivery_mode="chat_invite", min_comment_length=5, stop_words="спам"
    )
    for data in (
        comment(content="ок"),
        comment(number=2, content="это спам"),
        comment(source="video", number=3),
    ):
        client.post("/vk/callback", json=data)
    monkeypatch.setattr(main, "is_group_member", AsyncMock(return_value=False))
    client.post("/vk/callback", json=comment(number=4))
    replies.assert_not_called()
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0
        assert {row.status for row in session.query(db.ProcessedComment)} == {
            "too_short",
            "stop_word",
            "invite_unsupported",
            "not_member",
        }


def test_official_wall_reply_parameters(setup, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(vk_api, "call", call)
    asyncio.run(
        vk_api.reply_to_wall_comment(
            normalize_comment(comment(), 123), "Спасибо!", "stable-guid"
        )
    )
    call.assert_awaited_once_with(
        "wall.createComment",
        owner_id=-123,
        post_id=594,
        reply_to_comment=1,
        from_group=123,
        message="Спасибо!",
        guid="stable-guid",
    )
    with pytest.raises(ValueError):
        asyncio.run(
            vk_api.reply_to_wall_comment(
                normalize_comment(comment(source="video"), 123), "x", "g"
            )
        )


def test_parallel_comments_and_claims_do_not_duplicate(invitations, monkeypatch):
    _, sessions, replies, sent = invitations
    campaign(sessions, delivery_mode="chat_invite")

    async def slow_member(_):
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(main, "is_group_member", slow_member)
    monkeypatch.setattr(vk_api, "is_group_member", slow_member)

    async def dispatch(bodies):
        requests = []
        for body in bodies:
            request = AsyncMock()
            request.json.return_value = body
            requests.append(main.vk_callback(request))
        await asyncio.gather(*requests)

    asyncio.run(dispatch([comment(number=1), comment(number=2)]))
    assert replies.call_count == 1
    asyncio.run(dispatch([message("Подарок"), message("Подарок")]))
    assert sent.call_count == 1
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 1
        assert session.query(db.PromoDelivery).count() == 1


def test_test_mode_blocks_invitations_and_own_comments_are_ignored(invitations):
    client, sessions, replies, _ = invitations
    campaign(sessions, delivery_mode="chat_invite")
    with sessions() as session:
        db.save_settings(
            session, {"test_mode": "true", "test_trigger_phrase": "тестовое сообщение"}
        )
    client.post("/vk/callback", json=comment())
    client.post(
        "/vk/callback", json=comment(number=2, user=-123, content="тестовое сообщение")
    )
    replies.assert_not_called()
    client.post("/vk/callback", json=comment(number=3, content="Тестовое сообщение"))
    assert replies.call_count == 1


def test_additive_upgrade_preserves_campaigns_and_dialog_events(setup):
    _, sessions, _ = setup
    ident = campaign(sessions, post_id=594)
    with sessions() as session:
        session.add(
            db.DialogEvent(user_id=77, event_key="legacy", text="hello", status="done")
        )
        session.commit()
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE campaigns DROP COLUMN delivery_mode"))
        connection.execute(
            text("ALTER TABLE campaigns DROP COLUMN public_reply_variants")
        )
        connection.execute(text("ALTER TABLE dialog_events DROP COLUMN gift_id"))
    db.init_db()
    db.init_db()
    with sessions() as session:
        row = session.get(db.Campaign, ident)
        assert row.delivery_mode == "direct" and row.public_reply_variants == []
        event = session.query(db.DialogEvent).one()
        assert event.text == "hello" and event.gift_id is None
