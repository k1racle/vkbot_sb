from unittest.mock import AsyncMock

import pytest
from test_gifts import invitations as invitations
from test_gifts import comments as comments, setup as setup
from test_comments import campaign, event
from test_scenarios import db, login, vk_api


@pytest.mark.parametrize("mode,status", [("chat_only", "chat_mentioned"), ("chat_invite", "video_mentioned")])
def test_video_mention_when_dm_closed(invitations, monkeypatch, mode, status):
    client, sessions, replies, sent = invitations
    campaign(sessions, delivery_mode=mode)
    with sessions() as session:
        db.save_settings(session, {"video_mention_post_id": "456"})
    monkeypatch.setattr(vk_api, "video_reply_available", lambda: False)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=False))
    mention = AsyncMock()
    monkeypatch.setattr(vk_api, "mention_video_author", mention)
    body = event("video")
    assert client.post("/vk/callback", json=body).text == "ok"
    client.post("/vk/callback", json=body)
    client.post("/vk/callback", json=event("video", number=2))
    assert mention.call_count == (2 if mode == "chat_invite" else 1)
    assert mention.call_args.args[1] == 456
    assert "https://vk.me/club123" in mention.call_args.args[2]
    sent.assert_not_called()
    replies.assert_not_called()
    with sessions() as session:
        assert session.query(db.ProcessedComment).filter_by(comment_id=1).one().status == status
        assert session.query(db.PendingGift).count() == int(mode == "chat_invite")


def test_mention_setting_validation_and_preservation(invitations):
    client, sessions, _, _ = invitations
    login(client)
    client.post("/admin/settings", data={"chat_url": "", "video_mention_post_id": "456"})
    with sessions() as session:
        assert db.read_settings(session)["video_mention_post_id"] == "456"
    client.post("/admin/settings", data={"video_mention_post_id": "-999_456"})
    client.post("/admin/settings", data={"test_trigger_phrase": "тест"})
    with sessions() as session:
        assert db.read_settings(session)["video_mention_post_id"] == "456"


def test_mention_uses_wall_post_and_group_token(monkeypatch):
    import asyncio
    from app.comments import normalize_comment

    call = AsyncMock()
    monkeypatch.setattr(vk_api, "call", call)
    comment = normalize_comment(event("video", object_id=999), 123)
    asyncio.run(vk_api.mention_video_author(comment, 456, "Приходите в чат", "stable"))
    assert call.call_args.args == ("wall.createComment",)
    params = call.call_args.kwargs
    assert params["post_id"] == 456 and params["owner_id"] == -123
    assert params["message"].startswith("[id77|")
    assert "reply_to_comment" not in params


@pytest.mark.parametrize("unsubscribed", [False, True])
def test_dm_priority_and_unsubscribe(invitations, monkeypatch, unsubscribed):
    client, sessions, _, sent = invitations
    campaign(sessions, delivery_mode="chat_only")
    with sessions() as session:
        db.save_settings(session, {"video_mention_post_id": "456"})
        if unsubscribed:
            session.add(db.Client(user_id=77, unsubscribed=True))
            session.commit()
    monkeypatch.setattr(vk_api, "video_reply_available", lambda: False)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    mention = AsyncMock()
    monkeypatch.setattr(vk_api, "mention_video_author", mention)
    client.post("/vk/callback", json=event("video"))
    mention.assert_not_called()
    assert sent.call_count == int(not unsubscribed)
