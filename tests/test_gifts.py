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


def test_custom_chat_url_admin_round_trip_and_gift_claim(invitations):
    client, sessions, replies, sent = invitations
    login(client)
    url = "https://vk.me/sarkisian.brand?ref=gift&ref_source=wall"
    result = client.post("/admin/settings", data={"chat_url": "  " + url + "  "})
    assert "saved=1" in str(result.url)
    assert 'name="chat_url"' in result.text
    with sessions() as session:
        assert db.read_settings(session)["chat_url"] == url
    invite(client, sessions)
    assert url in replies.call_args.args[1]
    assert "{chat_url}" not in replies.call_args.args[1]
    assert "club123" not in replies.call_args.args[1]
    page = client.get("/admin?section=campaigns")
    assert "Изменить ссылку на чат" in page.text
    assert "sarkisian.brand?ref=gift&amp;ref_source=wall" in page.text
    db.init_db()  # Saved settings survive restart/schema initialization.
    with sessions() as session:
        assert gifts.resolve_chat_url(db.read_settings(session)) == url
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_args.args == (77, "Код ALL")


def test_chat_url_changes_only_future_invitations_and_blank_resets(invitations):
    client, sessions, replies, _ = invitations
    login(client)
    client.post("/admin/settings", data={"chat_url": "https://vk.me/first.project"})
    invite(client, sessions)
    old_text = replies.call_args.args[1]
    client.post("/admin/settings", data={"chat_url": "https://vk.me/second.project"})
    client.post("/vk/callback", json=comment(user=88, number=2))
    assert "https://vk.me/second.project" in replies.call_args.args[1]
    with sessions() as session:
        assert (
            session.query(db.PendingGift).filter_by(user_id=77).one().invitation_text
            == old_text
        )
    # A duplicate must not republish an old invitation under the new URL.
    client.post("/vk/callback", json=comment())
    assert replies.call_count == 2
    client.post("/admin/settings", data={"chat_url": ""})
    client.post("/vk/callback", json=comment(user=99, number=3))
    assert "https://vk.me/club123" in replies.call_args.args[1]
    with sessions() as session:
        assert db.read_settings(session)["chat_url"] == ""


def test_older_settings_form_does_not_erase_custom_chat_url(invitations):
    client, sessions, _, _ = invitations
    login(client)
    client.post("/admin/settings", data={"chat_url": "https://vk.me/custom"})
    client.post(
        "/admin/settings", data={"test_mode": "1", "test_trigger_phrase": "проверка"}
    )
    with sessions() as session:
        values = db.read_settings(session)
        assert values["chat_url"] == "https://vk.me/custom"
        assert values["test_mode"] == "true"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://vk.me/test",
        "vk.me/test",
        "//vk.me/test",
        "https://",
        "https://user:password@vk.me/test",
        "https://[broken",
        "https://vk.me:99999/test",
        "https://vk.me:0/test",
        "https://vk.me/test name",
        "https://vk.me/test\nother",
        'https://vk.me/"test',
        "https://vk.me/<test>",
        "https://vk.me/\\test",
        "https://vk.me/{chat_url}",
        "https://vk.me/" + "a" * 500,
    ],
)
def test_invalid_chat_url_preserves_all_settings(invitations, url):
    client, sessions, _, _ = invitations
    login(client)
    with sessions() as session:
        db.save_settings(
            session, {"chat_url": "https://vk.me/previous", "test_mode": "true"}
        )
    result = client.post("/admin/settings", data={"chat_url": url})
    assert "settings_error=chat_url" in str(result.url)
    with sessions() as session:
        values = db.read_settings(session)
        assert values["chat_url"] == "https://vk.me/previous"
        assert values["test_mode"] == "true"


def test_chat_url_requires_login_and_csrf_accepts_html_form(invitations):
    client, sessions, _, _ = invitations
    r = client.post(
        "/admin/settings",
        data={"chat_url": "https://vk.me/test"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"
    login(client)
    csrf = client.headers.pop("X-CSRF-Token")
    for invalid in ("", "wrong", "неверный"):
        r = client.post(
            "/admin/settings",
            data={"chat_url": "https://vk.me/test", "csrf_token": invalid},
        )
        assert r.status_code == 403
    with sessions() as session:
        assert "chat_url" not in db.read_settings(session)
    r = client.post(
        "/admin/settings", data={"chat_url": "https://vk.me/test", "csrf_token": csrf}
    )
    assert "saved=1" in str(r.url)
    with sessions() as session:
        assert db.read_settings(session)["chat_url"] == "https://vk.me/test"


def test_chat_url_defaults_follow_group_and_invalid_legacy_value_is_safe(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(gifts, "get_settings", lambda: SimpleNamespace(vk_group_id=456))
    assert gifts.resolve_chat_url({}) == "https://vk.me/club456"
    assert (
        gifts.resolve_chat_url({"chat_url": "javascript:alert(1)"})
        == "https://vk.me/club456"
    )
    assert (
        gifts.resolve_chat_url({"chat_url": "https://vk.me/other.project"})
        == "https://vk.me/other.project"
    )


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
    action = sent.call_args.kwargs["keyboard"]["buttons"][-1][0]["action"]
    assert action["label"] == "Проверить подписку"
    with sessions() as session:
        gift = session.query(db.PendingGift).one()
        assert gift.status == "pending" and gift.awaiting_subscription
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
    replies.assert_not_called()
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0
        assert {row.status for row in session.query(db.ProcessedComment)} == {
            "too_short",
            "stop_word",
            "invite_unsupported",
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


def joined(user=77, number=1, join_type="join"):
    return {
        "type": "group_join",
        "event_id": f"join-{user}-{number}",
        "group_id": 123,
        "secret": "test-secret",
        "object": {"user_id": user, "join_type": join_type},
    }


@pytest.fixture
def subscriptions(invitations, monkeypatch):
    client, sessions, replies, sent = invitations
    membership = AsyncMock(return_value=False)
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "is_group_member", membership)
    monkeypatch.setattr(vk_api, "is_group_member", membership)
    monkeypatch.setattr(vk_api, "is_messages_allowed", allowed)
    return client, sessions, replies, sent, membership, allowed


def test_nonmember_invited_then_start_join_sends_automatically(subscriptions):
    client, sessions, replies, sent, member, allowed = subscriptions
    invite(client, sessions)
    assert replies.call_count == 1
    sent.assert_not_called()
    member.assert_not_called()  # Invitations no longer exclude non-members.
    client.post("/vk/callback", json=message("", payload={"command": "start"}))
    assert "подпишитесь" in sent.call_args.args[1]
    buttons = sent.call_args.kwargs["keyboard"]["buttons"]
    assert buttons[0][0]["action"]["link"] == "https://vk.ru/club123"
    assert buttons[1][0]["action"]["label"] == "Проверить подписку"
    with sessions() as session:
        assert session.query(db.ProcessedComment).one().status == "waiting_subscription"
        assert session.query(db.PendingGift).one().awaiting_subscription
    db.init_db()  # Restart does not lose the wait or consent marker.
    member.return_value = True
    client.post("/vk/callback", json=joined())
    assert sent.call_count == 2 and sent.call_args.args[1] == "Код ALL"
    allowed.assert_awaited_once_with(77)
    for body in (joined(), joined(number=2), message("", payload={"command": "start"})):
        client.post("/vk/callback", json=body)
    assert sent.call_count == 2
    with sessions() as session:
        gift = session.query(db.PendingGift).one()
        assert gift.status == "sent" and not gift.awaiting_subscription
        assert session.query(db.PromoDelivery).count() == 1
        assert (
            session.query(db.DialogEvent).filter_by(kind="gift_join").one().status
            == "done"
        )


def test_join_never_sends_before_gift_request_or_without_comment(subscriptions):
    client, sessions, _, sent, member, allowed = subscriptions
    invite(client, sessions)
    member.return_value = True
    client.post("/vk/callback", json=joined())
    client.post("/vk/callback", json=joined(user=88))
    sent.assert_not_called()
    allowed.assert_not_called()
    with sessions() as session:
        assert not session.query(db.PendingGift).one().awaiting_subscription
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_args.args[1] == "Код ALL"  # Join-before-Start order also works.


def test_auto_delivery_preserves_manager_and_works_with_chat_disabled(subscriptions):
    client, sessions, _, sent, member, _ = subscriptions
    invite(client, sessions)
    with sessions() as session:
        session.add(
            db.Conversation(
                user_id=77,
                handoff=True,
                assigned_operator_id=99,
                node_id="question",
                variables={"size": "M"},
            )
        )
        db.save_settings(session, {"chat_enabled": "false"})
    client.post("/vk/callback", json=message("Начать"))
    member.return_value = True
    client.post("/vk/callback", json=joined())
    assert sent.call_args.args[1] == "Код ALL"
    with sessions() as session:
        row = session.get(db.Conversation, 77)
        assert row.handoff and row.assigned_operator_id == 99
        assert row.node_id == "question" and row.variables == {"size": "M"}
    login(client)
    assert "group_join" in client.get("/admin?section=campaigns").text
    assert "sent" in client.get("/admin?section=stats").text


def test_start_without_comment_never_creates_or_arms_a_gift(subscriptions):
    client, sessions, _, sent, member, allowed = subscriptions
    campaign(sessions, delivery_mode="chat_invite")
    client.post("/vk/callback", json=message("Начать"))
    assert sent.call_count == 1 and sent.call_args.args[1] != "Код ALL"
    member.return_value = True
    client.post("/vk/callback", json=joined())
    assert sent.call_count == 1
    allowed.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).count() == 0


@pytest.mark.parametrize("join_type", ["request", "unsure", "invalid"])
def test_unconfirmed_join_does_not_deliver(subscriptions, join_type):
    client, sessions, _, sent, member, allowed = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    sent.reset_mock()
    member.return_value = True
    client.post("/vk/callback", json=joined(join_type=join_type))
    sent.assert_not_called()
    allowed.assert_not_called()
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "pending"
    client.post("/vk/callback", json=joined(number=2, join_type="approved"))
    assert sent.call_args.args[1] == "Код ALL"


@pytest.mark.parametrize(
    "field,value", [("group_id", 999), ("secret", "wrong"), ("event_id", None)]
)
def test_invalid_join_cannot_trigger_delivery(subscriptions, field, value):
    client, sessions, _, sent, member, _ = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    member.return_value = True
    body = joined()
    body[field] = value
    client.post("/vk/callback", json=body)
    assert sent.call_count == 1
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "pending"


def test_join_rechecks_membership_and_consent(subscriptions):
    client, sessions, _, sent, member, allowed = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    client.post("/vk/callback", json=joined())  # Left again or stale event.
    assert sent.call_count == 1
    member.return_value = True
    allowed.return_value = False
    client.post("/vk/callback", json=joined(number=2))
    assert sent.call_count == 1
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "pending"
        assert session.query(db.ProcessedComment).one().status == "waiting_permission"
    # New explicit inbound request can retry after the user enables messages.
    client.post("/vk/callback", json=message("Подарок", number=2))
    assert sent.call_args.args[1] == "Код ALL"


@pytest.mark.parametrize("change", ["disable", "delete", "delivered"])
def test_join_respects_campaign_and_history_without_extra_notices(
    subscriptions, change
):
    client, sessions, _, sent, member, _ = subscriptions
    ident = invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    with sessions() as session:
        row = session.get(db.Campaign, ident)
        if change == "disable":
            row.enabled = False
        elif change == "delete":
            session.delete(row)
        else:
            session.add(db.PromoDelivery(user_id=77, campaign_id=ident))
        session.commit()
    member.return_value = True
    client.post("/vk/callback", json=joined())
    assert sent.call_count == 1
    with sessions() as session:
        assert session.query(db.PendingGift).one().status == "cancelled"


def test_failed_auto_send_keeps_payload_and_old_event_cannot_claim_next(subscriptions):
    client, sessions, _, sent, member, _ = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    member.return_value = True
    sent.side_effect = vk_api.VkApiError("6: Too many requests")
    with pytest.raises(vk_api.VkApiError):
        client.post("/vk/callback", json=joined())
    nonce = sent.call_args.kwargs["random_id"]
    sent.side_effect = None
    client.post("/vk/callback", json=message("Подарок", number=2))
    assert sent.call_args.kwargs["random_id"] == nonce
    campaign(sessions, post_id=888, delivery_mode="chat_invite")
    client.post("/vk/callback", json=comment(object_id=888, number=2))
    member.return_value = False
    client.post("/vk/callback", json=message("Подарок", number=3))
    member.return_value = True
    before = sent.call_count
    client.post("/vk/callback", json=joined())
    assert sent.call_count == before
    with sessions() as session:
        assert (
            session.query(db.PendingGift)
            .filter_by(status="pending", awaiting_subscription=True)
            .count()
            == 1
        )


def test_parallel_join_and_button_only_send_once(subscriptions):
    client, sessions, _, sent, member, _ = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=message("Начать"))
    member.return_value = True

    async def dispatch():
        requests = []
        for body in (joined(), message("Подарок", number=2), joined()):
            request = AsyncMock()
            request.json.return_value = body
            requests.append(main.vk_callback(request))
        await asyncio.gather(*requests)

    asyncio.run(dispatch())
    assert sum(call.args[1] == "Код ALL" for call in sent.call_args_list) == 1
    with sessions() as session:
        assert session.query(db.PromoDelivery).count() == 1


def test_upgrade_restores_only_previous_explicit_gift_requests(subscriptions):
    client, sessions, _, _, _, _ = subscriptions
    invite(client, sessions)
    client.post("/vk/callback", json=comment(user=88, number=2))
    client.post("/vk/callback", json=message("Начать"))
    with db.engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE pending_gifts DROP COLUMN awaiting_subscription")
        )
    db.init_db()
    db.init_db()
    with sessions() as session:
        assert (
            session.query(db.PendingGift)
            .filter_by(user_id=77)
            .one()
            .awaiting_subscription
        )
        assert (
            not session.query(db.PendingGift)
            .filter_by(user_id=88)
            .one()
            .awaiting_subscription
        )


@pytest.mark.parametrize(
    "response,allowed",
    [({"is_allowed": 1}, True), ({"is_allowed": 0}, False), ({}, False)],
)
def test_messages_permission_api(setup, monkeypatch, response, allowed):
    api = AsyncMock(return_value=response)
    monkeypatch.setattr(vk_api, "call", api)
    assert asyncio.run(vk_api.is_messages_allowed(77)) == allowed
    api.assert_awaited_once_with(
        "messages.isMessagesFromGroupAllowed", group_id=123, user_id=77
    )
