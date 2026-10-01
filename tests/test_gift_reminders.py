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
