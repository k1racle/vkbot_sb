from unittest.mock import AsyncMock

from test_comments import comments as comments, campaign, event
from test_scenarios import setup as setup, db, vk_api


def test_repeat_comment_resends_original_code_without_new_gift(comments, monkeypatch):
    client, sessions, _ = comments
    ident = campaign(sessions, delivery_mode="chat_invite")
    with sessions() as session:
        session.add(db.PromoDelivery(user_id=77, campaign_id=ident, promo_code="ORIGINAL"))
        session.get(db.Campaign, ident).promo_code = "CHANGED"
        session.commit()
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    sent = AsyncMock()
    monkeypatch.setattr(vk_api, "send_message", sent)
    body = event(object_id=900)
    client.post("/vk/callback", json=body)
    client.post("/vk/callback", json=body)
    client.post("/vk/callback", json=event(object_id=901))
    assert sent.call_count == 2
    assert "ORIGINAL" in sent.call_args.args[1]
    assert "CHANGED" not in sent.call_args.args[1]
    with sessions() as session:
        assert session.query(db.PromoDelivery).count() == 1
        assert session.query(db.PendingGift).count() == 0
        assert {r.status for r in session.query(db.ProcessedComment)} == {"gift_reminded_dm"}


def test_closed_dm_public_reminder_keeps_code_private(comments):
    client, sessions, _ = comments
    ident = campaign(sessions, delivery_mode="chat_invite")
    with sessions() as session:
        session.add(db.PromoDelivery(user_id=77, campaign_id=ident, promo_code="SECRET"))
        session.commit()
    client.post("/vk/callback", json=event())
    message = vk_api.reply_to_wall_comment.call_args.args[1]
    assert "SECRET" not in message
    assert "https://vk.me/club123" in message


def test_stale_pending_reminder_uses_confirmed_gift(comments, monkeypatch):
    import asyncio
    from app import gifts
    from app.comments import normalize_comment

    client, sessions, _ = comments
    ident = campaign(sessions, delivery_mode="chat_invite", one_promo_per_user=False)
    with sessions() as session:
        session.add(db.PendingGift(id="old-sent", user_id=77, campaign_id=ident,
            event_key="old", status="sent", active_key=None,
            invitation_text="old", delivery_payload={"text": "Ваш код ORIGINAL", "attachment": ""}))
        session.add(db.ProcessedComment(event_key="wall:-123:594:1", source_type="wall",
            owner_id=-123, post_id=594, comment_id=1, user_id=77, campaign_id=ident))
        session.commit()
        assert gifts.delivered(session, 77, session.get(db.Campaign, ident))
        monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
        sent = AsyncMock()
        monkeypatch.setattr(vk_api, "send_message", sent)
        asyncio.run(gifts.remind_delivered_gift(session, normalize_comment(event(), 123),
            session.get(db.Campaign, ident), pending=True))
        assert "ORIGINAL" in sent.call_args.args[1]
        assert "уже получили" in sent.call_args.args[1]
        assert "уже ждёт" not in sent.call_args.args[1]


def test_same_campaign_different_posts_stale_pending(comments, monkeypatch):
    client, sessions, _ = comments
    ident = campaign(sessions, delivery_mode="chat_invite", one_promo_per_user=False)
    with sessions() as session:
        session.add(db.PromoDelivery(user_id=77, campaign_id=ident, promo_code="ORIGINAL"))
        session.add(db.PendingGift(id="stale", user_id=77, campaign_id=ident,
            event_key="previous-post", status="pending", active_key=f"77:{ident}",
            invitation_text="old invitation"))
        session.commit()
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    sent = AsyncMock()
    monkeypatch.setattr(vk_api, "send_message", sent)
    client.post("/vk/callback", json=event(object_id=594))
    client.post("/vk/callback", json=event(object_id=595))
    assert sent.call_count == 2
    for call in sent.call_args_list:
        assert "ORIGINAL" in call.args[1]
        assert "уже ждёт" not in call.args[1]
