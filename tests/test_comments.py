import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

# Import the shared fixture first: it sets isolated credentials before app imports.
from test_scenarios import db, dialog, login, main, setup as setup


@pytest.fixture
def comments(setup, monkeypatch):
    client, sessions, _ = setup
    sent = AsyncMock()
    monkeypatch.setattr(main, "send_message", sent)
    monkeypatch.setattr(main, "get_user_name", AsyncMock(return_value="Анна"))
    monkeypatch.setattr(main, "is_group_member", AsyncMock(return_value=True))
    return client, sessions, sent


def event(source="wall", object_id=594, number=1, user=77, content="Хочу промокод"):
    return {
        "type": "wall_reply_new" if source == "wall" else "video_comment_new",
        "secret": "test-secret",
        "group_id": 123,
        "event_id": f"{source}:{object_id}:{number}",
        "object": {
            "id": number,
            "from_id": user,
            "text": content,
            "post_id" if source == "wall" else "video_id": object_id,
            "owner_id" if source == "wall" else "video_owner_id": -123,
        },
    }


def campaign(sessions, post_id=0, **kwargs):
    with sessions() as session:
        row = db.Campaign(
            post_id=post_id,
            title="Тест",
            promo_message="Код {promo_code}",
            promo_code="ALL",
            **kwargs,
        )
        session.add(row)
        session.commit()
        return row.id


def fields(**kwargs):
    return dict(
        title="Все публикации",
        post_id="",
        promo_code="ALL",
        shop_url="https://example.org",
        promo_message="Код {promo_code}",
        enabled="1",
        min_comment_length="1",
        **kwargs,
    )


def test_empty_post_creates_one_general_campaign_and_edits_it(comments):
    client, sessions, _ = comments
    login(client)
    response = client.post("/admin/campaigns", data=fields())
    assert "campaign_saved=1" in str(response.url)
    assert 'value="" placeholder="Пусто — все публикации"' in response.text
    assert "Все публикации</small>" in response.text
    with sessions() as session:
        row = session.query(db.Campaign).one()
        ident = row.id
        assert row.post_id == 0
    response = client.post("/admin/campaigns", data=fields())
    assert "campaign_error=duplicate_general" in str(response.url)
    changed = fields(campaign_id=str(ident))
    changed["promo_code"] = "CHANGED"
    client.post("/admin/campaigns", data=changed)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 1
        assert session.get(db.Campaign, ident).promo_code == "CHANGED"


@pytest.mark.parametrize("value", ["0", "-1", "594x", "1.5", "2147483648", "１２３"])
def test_invalid_post_never_silently_enables_all_publications(comments, value):
    client, sessions, _ = comments
    login(client)
    data = fields()
    data["post_id"] = value
    result = client.post("/admin/campaigns", data=data)
    assert "campaign_error=post_id" in str(result.url)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 0


def test_general_campaign_accepts_different_posts_and_videos(comments):
    client, sessions, sent = comments
    ident = campaign(sessions, one_promo_per_user=False)
    for source, object_id in (
        ("wall", 594),
        ("wall", 595),
        ("video", 594),
        ("video", 595),
    ):
        client.post("/vk/callback", json=event(source, object_id))
    assert sent.call_count == 4
    assert len({call.kwargs["random_id"] for call in sent.call_args_list}) == 4
    for source, object_id in (
        ("wall", 594),
        ("wall", 595),
        ("video", 594),
        ("video", 595),
    ):
        repeated = event(source, object_id)
        repeated["event_id"] = "new-delivery-id"
        client.post("/vk/callback", json=repeated)
    assert sent.call_count == 4
    with sessions() as session:
        rows = session.query(db.ProcessedComment).all()
        assert len(rows) == 4
        assert {row.source_type for row in rows} == {"wall", "video"}
        assert {row.status for row in rows} == {"sent"}
        assert {row.campaign_id for row in rows} == {ident}
    login(client)
    html = client.get("/admin?section=stats").text
    assert "video-123_594" in html and "wall-123_594" in html


def test_specific_campaign_wins_even_when_disabled_and_never_matches_video(comments):
    client, sessions, sent = comments
    campaign(sessions, one_promo_per_user=False)
    special = campaign(sessions, post_id=594, one_promo_per_user=False)
    with sessions() as session:
        session.get(db.Campaign, special).promo_code = "SPECIAL"
        session.commit()
    client.post("/vk/callback", json=event())
    assert sent.call_args.args[1] == "Код SPECIAL"
    client.post("/vk/callback", json=event("video"))
    assert sent.call_args.args[1] == "Код ALL"
    with sessions() as session:
        session.get(db.Campaign, special).enabled = False
        session.commit()
    client.post("/vk/callback", json=event(number=2))
    assert sent.call_count == 2
    with sessions() as session:
        assert (
            session.query(db.ProcessedComment)
            .filter_by(source_type="wall", comment_id=2)
            .one()
            .status
            == "campaign_disabled"
        )


@pytest.mark.parametrize(
    "enabled,status", [(None, "no_campaign"), (False, "campaign_disabled")]
)
def test_no_active_general_campaign_skips_unmatched_sources(comments, enabled, status):
    client, sessions, sent = comments
    campaign(sessions, post_id=594)
    if enabled is not None:
        campaign(sessions, enabled=enabled)
    client.post("/vk/callback", json=event("video"))
    client.post("/vk/callback", json=event("wall", 999))
    sent.assert_not_called()
    with sessions() as session:
        assert {row.status for row in session.query(db.ProcessedComment)} == {status}


def test_general_delivery_protection_shared_across_objects_and_dialog(comments):
    client, sessions, sent = comments
    ident = campaign(sessions)
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("video", 900))
    client.post("/vk/callback", json=event("wall", 901))
    assert sent.call_count == 1
    with sessions() as session:
        assert dialog.delivered(session, 77, session.get(db.Campaign, ident))
        session.add(db.PromoDelivery(user_id=88, campaign_id=ident))
        session.commit()
    client.post("/vk/callback", json=event("video", user=88, number=2))
    assert sent.call_count == 1
    campaign(sessions, post_id=777)
    client.post("/vk/callback", json=event(object_id=777))
    assert sent.call_count == 2  # A different campaign may issue another promo.


@pytest.mark.parametrize(
    "options,content,status",
    [
        ({"min_comment_length": 20}, "ок", "too_short"),
        ({"stop_words": "спам"}, "Это СПАМ", "stop_word"),
    ],
)
def test_video_applies_campaign_filters(comments, options, content, status):
    client, sessions, sent = comments
    campaign(sessions, **options)
    client.post("/vk/callback", json=event("video", content=content))
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.ProcessedComment).one().status == status


def test_video_applies_test_phrase_and_membership(comments, monkeypatch):
    client, sessions, sent = comments
    campaign(sessions)
    with sessions() as session:
        db.save_settings(
            session, {"test_mode": "true", "test_trigger_phrase": "тестовое сообщение"}
        )
    client.post("/vk/callback", json=event("video"))
    monkeypatch.setattr(main, "is_group_member", AsyncMock(return_value=False))
    client.post(
        "/vk/callback", json=event("video", number=2, content="тестовое сообщение")
    )
    sent.assert_not_called()
    with sessions() as session:
        assert {row.status for row in session.query(db.ProcessedComment)} == {
            "test_filtered",
            "not_member",
        }


def test_ignore_other_owners_groups_and_unsupported_types(comments):
    client, sessions, sent = comments
    campaign(sessions)
    variants = []
    body = event("video")
    body["object"]["video_owner_id"] = -999
    variants.append(body)
    body = event()
    body["group_id"] = 999
    variants.append(body)
    body = event()
    body["object"]["from_id"] = -999
    variants.append(body)
    body = event("video")
    body["type"] = "clip_comment_new"  # Not a documented VK callback event.
    variants.append(body)
    body = event("video")
    body["object"]["video_id"] = "invalid"
    variants.append(body)
    for body in variants:
        assert client.post("/vk/callback", json=body).text == "ok"
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.ProcessedComment).count() == 0


def test_wall_and_video_owner_variants():
    body = event()
    body["object"]["post_owner_id"] = body["object"].pop("owner_id")
    assert main.normalize_comment(body, 123).owner_id == -123
    body = event("video")
    body["object"]["owner_id"] = body["object"].pop("video_owner_id")
    assert main.normalize_comment(body, 123).source_type == "video"
    body["object"] = None
    assert main.normalize_comment(body, 123) is None


def test_parallel_wall_and_video_cannot_issue_same_campaign_twice(
    comments, monkeypatch
):
    _, sessions, sent = comments
    campaign(sessions)

    async def slow_member(_):
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(main, "is_group_member", slow_member)

    async def run():
        requests = []
        for source in ("wall", "video"):
            request = AsyncMock()
            request.json.return_value = event(source)
            requests.append(main.vk_callback(request))
        await asyncio.gather(*requests)

    asyncio.run(run())
    assert sent.call_count == 1


def test_migration_preserves_history_and_delivery_after_campaign_post_change(comments):
    client, sessions, sent = comments
    ident = campaign(sessions, post_id=594)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE processed_comments (id INTEGER PRIMARY KEY, comment_id INTEGER UNIQUE, post_id INTEGER, user_id INTEGER, status VARCHAR(32), error TEXT, created_at TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO processed_comments VALUES (1, 7, 594, 77, 'sent', NULL, CURRENT_TIMESTAMP), (2, 8, 999, 88, 'failed', 'KEEP', CURRENT_TIMESTAMP)"
            )
        )
    db.init_db()
    db.init_db()
    with sessions() as session:
        assert session.query(db.ProcessedComment).count() == 2
        row = session.query(db.ProcessedComment).filter_by(comment_id=7).one()
        assert row.campaign_id == ident and row.event_key == "wall:-123:594:7"
        assert dialog.delivered(session, 77, session.get(db.Campaign, ident))
        session.get(db.Campaign, ident).post_id = 777
        session.commit()
    db.init_db()
    client.post(
        "/vk/callback", json=event(number=7)
    )  # Previously sent event stays processed.
    client.post("/vk/callback", json=event(object_id=777, number=9))
    sent.assert_not_called()
    with sessions() as session:
        assert (
            session.query(db.ProcessedComment).filter_by(comment_id=9).one().status
            == "already_sent"
        )
        assert (
            session.query(db.ProcessedComment).filter_by(comment_id=8).one().error
            == "KEEP"
        )
        assert (
            session.execute(text("SELECT COUNT(*) FROM processed_comments")).scalar()
            == 2
        )
