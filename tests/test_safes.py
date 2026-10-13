import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_comments import comments as comments, event as comment, fields
from test_scenarios import db, login, vk_api, event as message, setup as setup
from app import safes
from test_projects import project_env as project_env, two_projects as two_projects


@pytest.fixture
def game(comments, monkeypatch):
    client, sessions, _ = comments
    instant = safes.now().replace(microsecond=0)
    monkeypatch.setattr(safes, "now", lambda: instant)
    with sessions() as session:
        row = db.Campaign(
            post_id=594,
            title="Сейф",
            delivery_mode="safe",
            enabled=True,
            safe_code="0047",
            safe_start_at=instant - timedelta(hours=1),
            safe_end_at=instant + timedelta(hours=1),
            safe_locked=True,
            promo_code="BONUS",
            shop_url="https://example.org",
            promo_message="Подарок {promo_code}",
            public_reply_variants=safes.DEFAULT_INVITATIONS,
        )
        session.add(row)
        session.commit()
        ident = row.id

    return client, sessions, ident, instant


def entry(instant, code="0047", user=77, number=1, offset=0):
    data = comment(content=code, user=user, number=number)
    data["object"]["date"] = safes.timestamp(instant) + offset
    return data


def check(client, sessions, user=77, number=10, attempt_id=None):
    if attempt_id is None:
        with sessions() as session:
            attempt_id = session.query(db.SafeAttempt).filter_by(user_id=user).one().id
    return client.post(
        "/vk/callback",
        json=message(
            "Проверить код",
            user=user,
            number=number,
            payload=json.dumps({"action": "safe_check", "attempt_id": attempt_id}),
        ),
    )


def test_leading_zero_one_attempt_bonus_button_and_single_winner(game):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    vk_api.send_message.assert_not_called()  # Permission denied: public invitation survives.
    assert "vk.me" in vk_api.reply_to_wall_comment.call_args.args[1]
    for code in ("1234", "0047"):
        client.post("/vk/callback", json=entry(instant, code, number=2))
    client.post("/vk/callback", json=message("Начать"))
    assert "зарегистрирована" in vk_api.send_message.call_args.args[1]
    assert "Проверить код" in str(vk_api.send_message.call_args.kwargs["keyboard"])
    assert check(client, sessions).text == "ok"
    assert any(
        "бокс твой" in call.args[1] for call in vk_api.send_message.call_args_list
    )
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        assert attempt.code == "0047" and attempt.comment_id == 1 and attempt.bonus_sent
        assert session.get(db.Campaign, ident).safe_winner_id == attempt.id
        assert session.query(db.PromoDelivery).count() == 1
    db.init_db()
    check(client, sessions, number=11)
    client.post("/vk/callback", json=entry(instant, user=88, number=3, offset=1))
    check(client, sessions, user=88, number=12)
    assert any(
        "уже открыли" in call.args[1] for call in vk_api.send_message.call_args_list
    )
    with sessions() as session:
        assert session.query(db.SafeAttempt).count() == 2
        assert session.query(db.PromoDelivery).count() == 2
        assert (
            session.get(db.Campaign, ident).safe_winner_id
            == session.query(db.SafeAttempt).filter_by(user_id=77).one().id
        )


@pytest.mark.parametrize(
    "code", ["123", "12345", "１２３４", "١٢٣٤", "код 0047", "0047 1234", "привет"]
)
def test_bad_format_does_not_consume_attempt(game, code):
    client, sessions, _, instant = game
    client.post("/vk/callback", json=entry(instant, code))
    with sessions() as session:
        assert session.query(db.SafeAttempt).count() == 0
    client.post("/vk/callback", json=entry(instant, "1111", number=2))
    check(client, sessions)
    assert "Подарок BONUS" == vk_api.send_message.call_args.args[1]


def test_wrong_first_cannot_be_replaced_by_right(game):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant, "1234"))
    client.post("/vk/callback", json=entry(instant, number=2))
    check(client, sessions)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
        assert session.query(db.SafeAttempt).one().code == "1234"


@pytest.mark.parametrize(
    "kind", ["wall_reply_edit", "wall_reply_delete", "wall_reply_restore"]
)
def test_edit_delete_restore_never_return_attempt(game, kind):
    client, sessions, ident, instant = game
    data = entry(instant)
    client.post("/vk/callback", json=data)
    data["type"] = kind
    data["object"]["text"] = "1234"
    if kind == "wall_reply_delete":
        data["object"].pop("from_id")
        data["object"]["user_id"] = 77
    client.post("/vk/callback", json=data)
    client.post("/vk/callback", json=entry(instant, number=2))
    check(client, sessions)
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().invalid_reason
        assert session.query(db.PromoDelivery).count() == 0
        assert session.get(db.Campaign, ident).safe_winner_id is None


def test_edit_before_new_is_still_invalid(game):
    client, sessions, _, instant = game
    edited = entry(instant)
    edited["type"] = "wall_reply_edit"
    client.post("/vk/callback", json=edited)
    client.post("/vk/callback", json=entry(instant))
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().invalid_reason == "edited"


def test_callback_order_and_button_order_do_not_change_chronology(game):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant, user=88, number=2, offset=2))
    client.post("/vk/callback", json=entry(instant, user=77, number=1))
    check(client, sessions, user=88)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
    check(client, sessions, user=77)
    with sessions() as session:
        winner = session.get(
            db.SafeAttempt, session.get(db.Campaign, ident).safe_winner_id
        )
        assert winner.user_id == 77


def test_reordered_first_comment_same_user(game):
    client, sessions, _, instant = game
    client.post("/vk/callback", json=entry(instant, number=2, offset=2))
    client.post("/vk/callback", json=entry(instant, "1111", number=1))
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().code == "1111"


def test_delayed_earlier_callback_cannot_create_a_second_winner(game):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant, user=88, number=2))
    check(client, sessions, user=88)
    # Events arriving after the committed award cannot transfer the prize.
    client.post("/vk/callback", json=entry(instant, user=77, number=1, offset=-1))
    check(client, sessions, user=77, number=11)
    with sessions() as session:
        winner = session.get(
            db.SafeAttempt, session.get(db.Campaign, ident).safe_winner_id
        )
        assert winner.user_id == 88
        assert session.query(db.PromoDelivery).count() == 2
    congratulations = [
        call
        for call in vk_api.send_message.call_args_list
        if "бокс твой" in call.args[1]
    ]
    assert len(congratulations) == 1 and congratulations[0].args[0] == 88


def test_membership_gate_and_foreign_button(game, monkeypatch):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    monkeypatch.setattr(vk_api, "is_group_member", AsyncMock(return_value=False))
    check(client, sessions)
    assert "подпишитесь" in vk_api.send_message.call_args.args[1]
    with sessions() as session:
        aid = session.query(db.SafeAttempt).one().id
        assert session.get(db.Campaign, ident).safe_winner_id is None
    check(client, sessions, user=88, attempt_id=aid)
    assert "не найдена" in vk_api.send_message.call_args.args[1]
    assert all(
        call.args[1] != "Подарок BONUS" for call in vk_api.send_message.call_args_list
    )


@pytest.mark.parametrize("corruption", ["missing", "unseen", "text", "author", "date"])
def test_missing_or_inconsistent_original_blocks_award(game, corruption):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    with sessions() as session:
        original = session.query(db.SafeComment).one()
        if corruption == "missing":
            session.delete(original)
        elif corruption == "unseen":
            original.original_seen = False
        elif corruption == "text":
            original.original_text = "9999"
        elif corruption == "author":
            original.user_id = 88
        else:
            original.published_at -= 1
        session.commit()
    check(client, sessions)
    assert "не удалось завершить" in vk_api.send_message.call_args.args[1]
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None


def test_edit_reverted_to_correct_code_stays_invalid_and_next_user_can_win(game):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    client.post("/vk/callback", json=entry(instant, user=88, number=2, offset=1))
    for code in ("1234", "0047"):
        edited = entry(instant, code)
        edited["type"] = "wall_reply_edit"
        client.post("/vk/callback", json=edited)
    check(client, sessions, user=88)
    with sessions() as session:
        assert (
            session.query(db.SafeAttempt).filter_by(user_id=77).one().invalid_reason
            == "edited"
        )
        assert (
            session.get(
                db.SafeAttempt, session.get(db.Campaign, ident).safe_winner_id
            ).user_id
            == 88
        )


def test_winner_survives_failed_send_and_late_deletion(game, monkeypatch):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    sent = vk_api.send_message
    sent.side_effect = vk_api.VkApiError("901: denied")
    with pytest.raises(vk_api.VkApiError):
        check(client, sessions)
    with sessions() as session:
        winner_id = session.get(db.Campaign, ident).safe_winner_id
        assert winner_id is not None
    sent.side_effect = None
    deleted = entry(instant)
    deleted["type"] = "wall_reply_delete"
    client.post("/vk/callback", json=deleted)
    check(client, sessions)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.safe_winner_id == winner_id and campaign.safe_notification_sent
        assert session.query(db.PromoDelivery).count() == 1


def test_db_unique_attempt_and_additive_migration(game):
    _, sessions, ident, instant = game
    db.init_db()
    db.init_db()
    with sessions() as session:
        for _ in range(2):
            session.add(
                db.SafeAttempt(
                    campaign_id=ident,
                    user_id=77,
                    code="0000",
                    comment_id=1,
                    published_at=1,
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_admin_create_validation_and_immutable_rules(comments):
    client, sessions, _ = comments
    login(client)
    instant = safes.now() + timedelta(hours=1)
    data = fields(
        delivery_mode="safe",
        safe_code="0047",
        safe_start_at=safes.local_date(instant),
        safe_end_at=safes.local_date(instant + timedelta(hours=1)),
    )
    data["post_id"] = "594"
    result = client.post("/admin/campaigns", data=data)
    assert "campaign_saved=1" in str(result.url), result.text
    assert "Секретный код" in result.text and "Участники сейфа" in result.text
    with sessions() as session:
        row = session.query(db.Campaign).one()
        assert row.safe_code == "0047" and row.safe_locked and row.one_promo_per_user
        data["campaign_id"] = str(row.id)
    data["safe_code"] = "9999"
    result = client.post("/admin/campaigns", data=data)
    assert "campaign_error=safe_locked" in str(result.url)
    with sessions() as session:
        assert session.query(db.Campaign).one().safe_code == "0047"


@pytest.mark.parametrize(
    "updates",
    [
        {"safe_code": "１２３４"},
        {"post_id": ""},
        {"safe_end_at": "bad"},
        {"safe_winner": ""},
    ],
)
def test_admin_rejects_invalid_safe(comments, updates):
    client, sessions, _ = comments
    login(client)
    instant = safes.now() + timedelta(hours=1)
    data = fields(
        delivery_mode="safe",
        safe_code="0047",
        safe_start_at=safes.local_date(instant),
        safe_end_at=safes.local_date(instant + timedelta(hours=1)),
    )
    data.update(post_id="594")
    data.update(updates)
    result = client.post("/admin/campaigns", data=data)
    assert "campaign_error=safe_settings" in str(result.url)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 0


def test_concurrent_checks_leave_only_one_winner(game, monkeypatch):
    client, sessions, ident, instant = game
    for user, number in [(77, 1), (88, 2)]:
        client.post("/vk/callback", json=entry(instant, user=user, number=number))

    async def run():
        gate = asyncio.Event()
        calls = 0

        async def overlap(user):
            nonlocal calls
            calls += 1
            if calls == 2:
                gate.set()
            await gate.wait()
            return True

        monkeypatch.setattr(vk_api, "is_group_member", overlap)
        with sessions() as session:
            ids = [
                r.id
                for r in session.query(db.SafeAttempt).order_by(
                    db.SafeAttempt.comment_id
                )
            ]
        return await asyncio.gather(
            *(safes.claim_winner(aid) for aid in ids), return_exceptions=True
        )

    results = asyncio.run(run())
    # A competing revision may ask one caller to retry. It can never award 88.
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        if campaign.safe_winner_id:
            assert session.get(db.SafeAttempt, campaign.safe_winner_id).user_id == 77
    assert results[1] != "winner"
    with sessions() as session:
        first = session.query(db.SafeAttempt).filter_by(user_id=77).one().id
    assert asyncio.run(safes.claim_winner(first)) == "winner"
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id == first


@pytest.mark.parametrize("kind", ["wall_reply_edit", "wall_reply_delete", "earlier"])
def test_callback_during_verification_prevents_stale_award(game, monkeypatch, kind):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    calls = 0

    async def changing(user):
        nonlocal calls
        calls += 1
        if calls == 2:  # Change history after claim_winner reads its revision.
            data = entry(instant)
            if kind == "earlier":
                data = entry(instant, user=88, number=9, offset=-1)
            else:
                data["type"] = kind
            safes.register_comment(data)
        return True

    monkeypatch.setattr(vk_api, "is_group_member", changing)
    check(client, sessions)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
    assert "не удалось завершить" in vk_api.send_message.call_args.args[1]


def test_unavailable_vk_does_not_award_and_button_can_retry(game, monkeypatch):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    membership = vk_api.is_group_member
    monkeypatch.setattr(
        vk_api,
        "is_group_member",
        AsyncMock(side_effect=[True, vk_api.VkApiError("6: Too many requests")]),
    )
    check(client, sessions)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
        assert "Too many requests" in session.query(db.SafeAttempt).one().error
    monkeypatch.setattr(vk_api, "is_group_member", membership)
    check(client, sessions, number=11)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id


def test_permission_dm_retry_keeps_invitation_id_and_attempt(game, monkeypatch):
    client, sessions, _, instant = game
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    vk_api.send_message.side_effect = vk_api.VkApiError("901: denied")
    client.post("/vk/callback", json=entry(instant))
    initial = vk_api.send_message.call_args.kwargs["random_id"]
    vk_api.send_message.side_effect = None
    client.post("/vk/callback", json=entry(instant))
    assert vk_api.send_message.call_args.kwargs["random_id"] == initial
    assert vk_api.reply_to_wall_comment.call_count == 1
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().invitation_sent


def test_bonus_timeout_retries_exact_payload_without_new_delivery(game):
    client, sessions, _, instant = game
    client.post("/vk/callback", json=entry(instant, "1234"))
    vk_api.send_message.side_effect = [None, vk_api.VkApiError("10: retry")]
    with pytest.raises(vk_api.VkApiError):
        check(client, sessions)
    initial = vk_api.send_message.call_args
    with sessions() as session:
        campaign = session.query(db.Campaign).one()
        campaign.promo_message = "CHANGED"
        session.commit()
    vk_api.send_message.side_effect = None
    check(client, sessions, number=11)
    assert vk_api.send_message.call_args == initial
    with sessions() as session:
        assert session.query(db.PromoDelivery).count() == 1


def test_dates_pause_and_deleted_campaign(game):
    client, sessions, ident, instant = game
    for offset in (-3601, 3600):
        client.post(
            "/vk/callback", json=entry(instant, number=abs(offset), offset=offset)
        )
    with sessions() as session:
        assert session.query(db.SafeAttempt).count() == 0
        campaign = session.get(db.Campaign, ident)
        campaign.enabled = False
        session.commit()
    client.post("/vk/callback", json=entry(instant))
    vk_api.reply_to_wall_comment.assert_not_called()
    check(client, sessions)
    assert "приостановлена" in vk_api.send_message.call_args.args[1]
    with sessions() as session:
        from app.recycle import remove_campaign

        remove_campaign(session, session.get(db.Campaign, ident))
        session.commit()
    check(client, sessions, number=11)
    assert "не найдена" in vk_api.send_message.call_args.args[1]


def test_upgrade_from_old_schema_preserves_campaign(setup):
    _, sessions, _ = setup
    with sessions() as session:
        campaign = db.Campaign(post_id=10, title="Existing", promo_code="KEEP")
        session.add(campaign)
        session.commit()
        ident = campaign.id
    with db.engine.begin() as connection:
        for column in db.Campaign.__table__.columns:
            if column.name.startswith("safe_"):
                connection.execute(
                    text(f"ALTER TABLE campaigns DROP COLUMN {column.name}")
                )
        connection.execute(
            text("ALTER TABLE dialog_events DROP COLUMN safe_attempt_id")
        )
        connection.execute(text("DROP TABLE safe_attempts"))
        connection.execute(text("DROP TABLE safe_comments"))
    db.init_db()
    db.init_db()
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.promo_code == "KEEP" and campaign.delivery_mode == "direct"
        assert not campaign.safe_locked and campaign.safe_winner_id is None


def test_safe_not_available_as_scenario_promo(game):
    client, _, ident, _ = game
    login(client)
    assert all(
        row["id"] != ident
        for row in client.get("/admin/api/scenarios").json()["campaigns"]
    )


@pytest.mark.parametrize("user_token", ["", "unusable-ip-bound-user-token"])
def test_safe_uses_only_group_token_and_projects_have_independent_winners(
    two_projects, user_token
):
    from test_projects import (
        callback,
        comment as project_comment,
        message as project_message,
        configure,
        session_for,
    )

    env, first, second = two_projects
    first = configure(env.client, first, video_token=user_token)
    second = configure(env.client, second, video_token=user_token)
    # The old extra check used a user token and failed with this live VK error.
    env.network.replies["wall.getComments"] = {
        "error": {
            "error_code": 5,
            "error_msg": "User authorization failed: access_token was given to another ip address",
        }
    }
    env.network.calls.clear()
    instant = safes.now().replace(microsecond=0)
    for project in (first, second):
        with session_for(project) as session:
            session.add(
                db.Campaign(
                    post_id=55,
                    title=project.name,
                    delivery_mode="safe",
                    safe_locked=True,
                    safe_code="0047",
                    safe_start_at=instant - timedelta(hours=1),
                    safe_end_at=instant + timedelta(hours=1),
                    promo_code=project.name,
                    promo_message="Bonus {promo_code}",
                    public_reply_variants=safes.DEFAULT_INVITATIONS,
                )
            )
            session.commit()

    for project in (first, second):
        data = project_comment(project)
        data["object"].update(text="0047", date=safes.timestamp(instant))
        assert callback(env.client, project, data).text == "ok"
        with session_for(project) as session:
            attempt = session.query(db.SafeAttempt).one()
            aid = attempt.id
            attempt.error = "5: User authorization failed: access_token was given to another ip address"
            session.commit()
        data = project_message(project, text="Проверить код", number=9)
        data["object"]["message"]["payload"] = json.dumps(
            {"action": "safe_check", "attempt_id": aid}
        )
        assert callback(env.client, project, data).text == "ok"
        with session_for(project) as session:
            assert session.query(db.Campaign).one().safe_winner_id == aid
            assert session.query(db.SafeAttempt).one().bonus_sent
            assert session.query(db.SafeAttempt).one().error == ""
            assert session.query(db.PromoDelivery).one().promo_code == project.name
    assert all(c["method"] != "wall.getComments" for c in env.network.calls)
    for project in (first, second):
        calls = [c for c in env.network.calls if c["project_id"] == project.id]
        assert calls
        assert all(c["params"]["access_token"] == project.token for c in calls)


def test_missing_optional_owner_still_uses_safe_and_never_regular_gift(game):
    client, sessions, _, instant = game
    data = entry(instant, "1234")
    data["object"].pop("owner_id")
    client.post("/vk/callback", json=data)
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().code == "1234"
        assert session.query(db.PromoDelivery).count() == 0
    vk_api.send_message.assert_not_called()


def test_subscription_lost_during_verification_does_not_award_bonus(game, monkeypatch):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    monkeypatch.setattr(vk_api, "is_group_member", AsyncMock(side_effect=[True, False]))
    check(client, sessions)
    assert "Подписка не подтвердилась" in vk_api.send_message.call_args.args[1]
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
        assert session.query(db.PromoDelivery).count() == 0
