import asyncio
import json
import re
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import httpx
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


def managers(sessions, ids):
    with sessions() as session:
        db.save_settings(session, {"operator_user_id": ids})
        session.commit()


def test_confirmed_winner_notifies_each_manager_once_with_links(game):
    client, sessions, ident, instant = game
    managers(sessions, "99, 100, 99")
    client.post("/vk/callback", json=entry(instant))
    vk_api.send_message.assert_not_called()
    check(client, sessions)
    assert vk_api.send_message.call_args_list[0].args[0] == 77
    db.init_db()
    check(client, sessions, number=11)
    notifications = [
        c for c in vk_api.send_message.call_args_list if c.args[0] in {99, 100}
    ]
    assert len(notifications) == 2
    assert {c.args[0] for c in notifications} == {99, 100}
    for call in notifications:
        assert "Кампания: Сейф" in call.args[1]
        assert "https://vk.com/id77" in call.args[1]
        assert "https://vk.com/wall-123_594?reply=1" in call.args[1]
        assert "0047" in call.args[1] and "бокс" in call.args[1]
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_notification_sent
        assert all(
            n.sent and not n.error for n in session.query(db.SafeManagerNotification)
        )
        assert session.query(db.PromoDelivery).count() == 0


def test_manager_failure_does_not_block_winner_and_retry_keeps_payload(game):
    client, sessions, ident, instant = game
    managers(sessions, "99, 100")
    client.post("/vk/callback", json=entry(instant))

    async def send(user, text, **kwargs):
        if user == 99:
            raise httpx.ReadTimeout("notification timed out")

    vk_api.send_message.side_effect = send
    check(client, sessions)
    original = next(c for c in vk_api.send_message.call_args_list if c.args[0] == 99)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.safe_winner_id and campaign.safe_notification_sent
        assert "timed out" in session.get(db.SafeManagerNotification, (ident, 99)).error
        assert session.get(db.SafeManagerNotification, (ident, 100)).sent
        campaign.title = "Changed title"
        session.commit()
    vk_api.send_message.side_effect = None
    vk_api.send_message.reset_mock()
    login(client)
    for _ in range(2):
        page = client.post(f"/admin/campaigns/{ident}/notify-safe-managers")
        assert page.status_code == 200
    vk_api.send_message.assert_called_once()
    assert vk_api.send_message.call_args == original
    assert "Уведомление отправлено" in page.text
    with sessions() as session:
        assert all(
            n.sent and not n.error for n in session.query(db.SafeManagerNotification)
        )


def test_managers_notified_even_if_congratulations_cannot_be_delivered(game):
    client, sessions, ident, instant = game
    managers(sessions, "99")
    client.post("/vk/callback", json=entry(instant))

    async def send(user, text, **kwargs):
        if user == 77:
            raise vk_api.VkApiError("901: messages denied")

    vk_api.send_message.side_effect = send
    with pytest.raises(vk_api.VkApiError):
        check(client, sessions)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.safe_winner_id and not campaign.safe_notification_sent
        assert session.get(db.SafeManagerNotification, (ident, 99)).sent
    vk_api.send_message.side_effect = None
    check(client, sessions, number=11)
    assert sum(c.args[0] == 99 for c in vk_api.send_message.call_args_list) == 1


def test_missing_managers_can_be_configured_after_win_without_resending_prize(game):
    client, sessions, ident, instant = game
    managers(sessions, "")
    client.post("/vk/callback", json=entry(instant))
    check(client, sessions)
    with sessions() as session:
        assert "Укажите ID" in session.get(db.Campaign, ident).safe_manager_error
        assert session.query(db.SafeManagerNotification).count() == 0
    managers(sessions, "99")
    vk_api.send_message.reset_mock()
    login(client)
    assert (
        client.post(f"/admin/campaigns/{ident}/notify-safe-managers").status_code == 200
    )
    vk_api.send_message.assert_called_once()
    assert vk_api.send_message.call_args.args[0] == 99
    with sessions() as session:
        assert not session.get(db.Campaign, ident).safe_manager_error


@pytest.mark.parametrize("condition", ["no_winner", "paused", "deleted", "test_mode"])
def test_manager_notifications_respect_campaign_state_and_test_mode(game, condition):
    client, sessions, ident, instant = game
    managers(sessions, "99")
    client.post("/vk/callback", json=entry(instant, "1234"))
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        if condition != "no_winner":
            campaign.safe_winner_id = session.query(db.SafeAttempt).one().id
        if condition == "paused":
            campaign.enabled = False
        if condition == "deleted":
            campaign.is_deleted = True
        if condition == "test_mode":
            db.save_settings(session, {"test_mode": "true"})
        session.commit()
    asyncio.run(safes.notify_winner_managers(ident))
    vk_api.send_message.assert_not_called()
    with sessions() as session:
        assert session.query(db.SafeManagerNotification).count() == 0


def test_wrong_code_never_notifies_managers(game):
    client, sessions, _, instant = game
    managers(sessions, "99")
    client.post("/vk/callback", json=entry(instant, "1234"))
    check(client, sessions)
    assert all(c.args[0] == 77 for c in vk_api.send_message.call_args_list)
    with sessions() as session:
        assert session.query(db.SafeManagerNotification).count() == 0


def test_notification_retry_skips_removed_manager(game):
    client, sessions, ident, instant = game
    managers(sessions, "99")
    client.post("/vk/callback", json=entry(instant))

    async def send(user, text, **kwargs):
        if user == 99:
            raise vk_api.VkApiError("901: denied")

    vk_api.send_message.side_effect = send
    check(client, sessions)
    managers(sessions, "100")
    vk_api.send_message.side_effect = None
    vk_api.send_message.reset_mock()
    asyncio.run(safes.notify_winner_managers(ident))
    vk_api.send_message.assert_called_once()
    assert vk_api.send_message.call_args.args[0] == 100
    with sessions() as session:
        assert not session.get(db.SafeManagerNotification, (ident, 99)).sent
        assert session.get(db.SafeManagerNotification, (ident, 100)).sent


def test_notification_migration_preserves_existing_winner(game):
    client, sessions, ident, instant = game
    managers(sessions, "")
    client.post("/vk/callback", json=entry(instant))
    check(client, sessions)
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE safe_manager_notifications"))
        connection.execute(text("ALTER TABLE campaigns DROP COLUMN safe_manager_error"))
    db.init_db()
    db.init_db()
    managers(sessions, "99")
    vk_api.send_message.reset_mock()
    asyncio.run(safes.notify_winner_managers(ident))
    vk_api.send_message.assert_called_once()
    assert vk_api.send_message.call_args.args[0] == 99
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.safe_winner_id == session.query(db.SafeAttempt).one().id
        assert campaign.safe_notification_sent and not campaign.safe_manager_error


def test_overlapping_manager_sends_use_same_idempotency_key_and_message(game):
    client, sessions, ident, instant = game
    managers(sessions, "")
    client.post("/vk/callback", json=entry(instant))
    check(client, sessions)
    managers(sessions, "99")
    vk_api.send_message.reset_mock()

    async def run():
        gate = asyncio.Event()
        count = 0

        async def send(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=2)

        vk_api.send_message.side_effect = send
        await asyncio.gather(
            safes.notify_winner_managers(ident), safes.notify_winner_managers(ident)
        )

    asyncio.run(run())
    first, second = vk_api.send_message.call_args_list
    assert first == second
    with sessions() as session:
        assert session.query(db.SafeManagerNotification).one().sent


def test_notification_retry_requires_login_and_csrf(game):
    client, _, ident, _ = game
    route = f"/admin/campaigns/{ident}/notify-safe-managers"
    assert client.post(route, follow_redirects=False).status_code == 303
    login(client)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(route).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/admin/campaigns/99999/notify-safe-managers").status_code == 404
    vk_api.send_message.assert_not_called()


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
        assert attempt.code == "0047" and attempt.comment_id == 1
        assert not attempt.bonus_sent and not attempt.bonus_payload
        assert session.get(db.Campaign, ident).safe_winner_id == attempt.id
        assert session.query(db.PromoDelivery).count() == 0
    db.init_db()
    check(client, sessions, number=11)
    client.post("/vk/callback", json=entry(instant, user=88, number=3, offset=1))
    check(client, sessions, user=88, number=12)
    assert any(
        "уже открыли" in call.args[1] for call in vk_api.send_message.call_args_list
    )
    with sessions() as session:
        assert session.query(db.SafeAttempt).count() == 2
        assert session.query(db.PromoDelivery).one().user_id == 88
        assert (
            session.get(db.Campaign, ident).safe_winner_id
            == session.query(db.SafeAttempt).filter_by(user_id=77).one().id
        )
    winner_messages = [
        call.args[1]
        for call in vk_api.send_message.call_args_list
        if call.args[0] == 77
    ]
    assert sum("С вами свяжется менеджер" in text for text in winner_messages) == 2
    assert all("BONUS" not in text for text in winner_messages)


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
    check(client, sessions, number=11)
    with sessions() as session:
        assert session.get(db.Campaign, ident).safe_winner_id is None
        assert session.query(db.SafeAttempt).one().code == "1234"
        assert session.query(db.PromoDelivery).one().user_id == 77
    assert (
        sum(
            call.args[1] == "Подарок BONUS"
            for call in vk_api.send_message.call_args_list
        )
        == 1
    )


def test_winner_uses_saved_greeting_without_preparing_promo_or_attachment(
    game, monkeypatch
):
    client, sessions, ident, instant = game
    upload = AsyncMock()
    monkeypatch.setattr(vk_api, "upload_file_for_message", upload)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        campaign.safe_messages = {"winner": "Поздравляем с выигрышем бокса!"}
        campaign.attachment_path = "must-not-be-uploaded.pdf"
        session.commit()
    client.post("/vk/callback", json=entry(instant))
    check(client, sessions)
    assert vk_api.send_message.call_count == 1
    text = vk_api.send_message.call_args.args[1]
    assert text.startswith("Поздравляем с выигрышем бокса!")
    assert "С вами свяжется менеджер" in text
    assert "BONUS" not in text
    upload.assert_not_called()
    vk_api.get_user_name.assert_not_called()
    login(client)
    page = client.get(f"/admin?section=campaigns&campaign_id={ident}")
    assert "Не предусмотрен — выигран бокс" in page.text


@pytest.mark.parametrize("already_sent", [False, True])
def test_existing_winner_never_resumes_old_bonus_delivery(game, already_sent):
    client, sessions, ident, instant = game
    client.post("/vk/callback", json=entry(instant))
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        campaign = session.get(db.Campaign, ident)
        campaign.safe_winner_id = attempt.id
        campaign.safe_notification_sent = True
        attempt.bonus_payload = {
            "text": "Подарок BONUS",
            "promo_code": "BONUS",
            "attachment": "doc123_456",
        }
        attempt.bonus_sent = already_sent
        if already_sent:
            session.add(
                db.PromoDelivery(user_id=77, campaign_id=ident, promo_code="BONUS")
            )
        session.commit()
    check(client, sessions)
    assert vk_api.send_message.call_count == 1
    assert "С вами свяжется менеджер" in vk_api.send_message.call_args.args[1]
    assert "BONUS" not in vk_api.send_message.call_args.args[1]
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().bonus_sent == already_sent
        assert session.query(db.PromoDelivery).count() == int(already_sent)


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
        assert session.query(db.PromoDelivery).one().user_id == 77
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
        assert session.query(db.PromoDelivery).count() == 0
        assert not session.get(db.SafeAttempt, winner_id).bonus_sent


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


@pytest.fixture
def editable_game(game):
    client, sessions, ident, instant = game
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        # Admin dates have minute precision, as in a campaign created by the form.
        campaign.safe_start_at = campaign.safe_start_at.replace(second=0)
        campaign.safe_end_at = campaign.safe_end_at.replace(second=0)
        data = fields(
            campaign_id=str(ident),
            delivery_mode="safe",
            safe_code=campaign.safe_code,
            safe_start_at=safes.local_date(campaign.safe_start_at),
            safe_end_at=safes.local_date(campaign.safe_end_at),
        )
        data.update(
            title=campaign.title,
            post_id=str(campaign.post_id),
            promo_code=campaign.promo_code,
            promo_message=campaign.promo_message,
        )
        session.commit()
    login(client)
    return client, sessions, ident, instant, data


@pytest.mark.parametrize("expired", [False, True])
def test_admin_extend_safe_preserves_attempts_and_accepts_new_codes(
    editable_game, monkeypatch, expired
):
    client, sessions, ident, instant, data = editable_game
    old_end = safes.parse_date(data["safe_end_at"])
    extended_time = old_end + timedelta(minutes=5)
    client.post("/vk/callback", json=entry(instant, "1234"))
    check(client, sessions)
    with sessions() as session:
        first_id = session.query(db.SafeAttempt).one().id
        bonus_payload = session.get(db.SafeAttempt, first_id).bonus_payload

    if expired:
        monkeypatch.setattr(safes, "now", lambda: extended_time)
        client.post("/vk/callback", json=entry(extended_time, user=88, number=2))
        with sessions() as session:
            assert session.query(db.SafeAttempt).count() == 1
    with sessions() as session:
        revision = session.get(db.Campaign, ident).safe_revision
    data["safe_end_at"] = safes.local_date(old_end + timedelta(days=1))
    response = client.post("/admin/campaigns", data=data)
    assert "campaign_saved=1" in str(response.url)
    with sessions() as session:
        campaign = session.query(db.Campaign).one()
        assert campaign.id == ident and campaign.enabled and campaign.safe_locked
        assert campaign.safe_start_at == safes.parse_date(data["safe_start_at"])
        assert campaign.safe_end_at == old_end + timedelta(days=1)
        assert campaign.safe_revision == revision + 1
        assert campaign.safe_winner_id is None
        first = session.query(db.SafeAttempt).one()
        assert first.id == first_id and first.code == "1234" and first.bonus_sent
        assert first.bonus_payload == bonus_payload
        assert session.query(db.PromoDelivery).count() == 1

    monkeypatch.setattr(safes, "now", lambda: extended_time)
    # An existing participant cannot replace their first code after extension.
    client.post("/vk/callback", json=entry(extended_time, number=3))
    check(client, sessions, number=11)
    client.post("/vk/callback", json=entry(extended_time, user=88, number=4))
    check(client, sessions, user=88, number=12)
    # The updated end is still exclusive.
    client.post(
        "/vk/callback",
        json=entry(old_end + timedelta(days=1), user=100, number=5),
    )
    with sessions() as session:
        assert session.query(db.SafeAttempt).count() == 2
        first = session.get(db.SafeAttempt, first_id)
        assert first.comment_id == 1 and first.code == "1234"
        winner = session.get(
            db.SafeAttempt, session.get(db.Campaign, ident).safe_winner_id
        )
        assert winner.user_id == 88 and winner.comment_id == 4
        assert session.query(db.PromoDelivery).one().user_id == 77


def test_repeated_extension_preserves_winner_and_manager_notification(editable_game):
    client, sessions, ident, instant, data = editable_game
    managers(sessions, "99")
    client.post("/vk/callback", json=entry(instant))
    check(client, sessions)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        winner_id, finalized_at = campaign.safe_winner_id, campaign.safe_finalized_at
        notification = session.get(db.SafeManagerNotification, (ident, 99)).message
    old_end = safes.parse_date(data["safe_end_at"])
    vk_api.send_message.reset_mock()
    for days in (1, 2):
        data["safe_end_at"] = safes.local_date(old_end + timedelta(days=days))
        response = client.post("/admin/campaigns", data=data)
        assert "campaign_saved=1" in str(response.url)
    vk_api.send_message.assert_not_called()
    client.post(
        "/vk/callback", json=entry(old_end + timedelta(days=1), user=88, number=2)
    )
    check(client, sessions, user=88)
    check(client, sessions, number=11)
    assert all(call.args[0] != 99 for call in vk_api.send_message.call_args_list)
    with sessions() as session:
        campaign = session.get(db.Campaign, ident)
        assert campaign.safe_winner_id == winner_id
        assert campaign.safe_finalized_at == finalized_at
        assert campaign.safe_notification_sent
        notice = session.query(db.SafeManagerNotification).one()
        assert notice.sent and notice.message == notification
        assert session.query(db.PromoDelivery).one().user_id == 88


@pytest.mark.parametrize("paused", [False, True])
def test_safe_end_is_editable_and_unchanged_date_can_be_saved(editable_game, paused):
    client, sessions, ident, _, data = editable_game
    if paused:
        data["enabled"] = ""
        with sessions() as session:
            session.get(db.Campaign, ident).enabled = False
            session.commit()
    page = client.get(f"/admin?section=campaigns&campaign_id={ident}")
    end_input = re.search(r'<input[^>]+name="safe_end_at"[^>]*>', page.text)[0]
    assert "readonly" not in end_input and "disabled" not in end_input
    assert f'min="{data["safe_end_at"]}"' in end_input
    start_input = re.search(r'<input[^>]+name="safe_start_at"[^>]*>', page.text)[0]
    assert "readonly" in start_input
    for extra_days in (0, 1):
        data["safe_end_at"] = safes.local_date(
            safes.parse_date(data["safe_end_at"]) + timedelta(days=extra_days)
        )
        response = client.post("/admin/campaigns", data=data)
        assert "campaign_saved=1" in str(response.url)
        with sessions() as session:
            campaign = session.get(db.Campaign, ident)
            assert campaign.enabled is not paused
            assert campaign.safe_end_at == safes.parse_date(data["safe_end_at"])


@pytest.mark.parametrize(
    "field,error",
    [("safe_start_at", "safe_locked"), ("safe_end_at", "safe_end_earlier")],
)
def test_started_safe_rejects_start_change_and_shorter_end(editable_game, field, error):
    client, sessions, ident, _, data = editable_game
    original = safes.parse_date(data[field])
    data[field] = safes.local_date(original - timedelta(minutes=1))
    response = client.post("/admin/campaigns", data=data)
    assert f"campaign_error={error}" in str(response.url)
    assert f"campaign_id={ident}" in str(response.url)
    with sessions() as session:
        assert getattr(session.get(db.Campaign, ident), field) == original


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
    with sessions() as session:
        assert "901" in session.query(db.SafeAttempt).one().invitation_error
    vk_api.send_message.side_effect = None
    client.post("/vk/callback", json=entry(instant))
    assert vk_api.send_message.call_args.kwargs["random_id"] == initial
    assert vk_api.reply_to_wall_comment.call_count == 1
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().invitation_sent
        assert session.query(db.SafeAttempt).one().invitation_error == ""


@pytest.mark.parametrize(
    "error", [vk_api.VkApiError("15: Access denied"), httpx.ReadTimeout("timeout")]
)
def test_public_failure_blocks_dm_and_survives_bonus_then_admin_can_retry(
    game, monkeypatch, error
):
    client, sessions, ident, instant = game
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    reply = vk_api.reply_to_wall_comment
    reply.side_effect = error
    client.post("/vk/callback", json=entry(instant, "1234"))
    initial = reply.call_args
    vk_api.send_message.assert_not_called()
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        aid = attempt.id
        assert not attempt.public_sent and not attempt.invitation_sent
        assert str(error) in attempt.public_error
    # A participant may enter the chat independently. This cannot erase the wall error.
    check(client, sessions)
    with sessions() as session:
        assert session.query(db.SafeAttempt).one().bonus_sent
        assert str(error) in session.query(db.SafeAttempt).one().public_error
    login(client)
    page = client.get(f"/admin?section=campaigns&campaign_id={ident}")
    assert "Ответ не отправлен" in page.text and str(error) in page.text
    assert "Повторить ответ под постом" in page.text
    reply.side_effect, reply.return_value = None, 2042
    vk_api.send_message.reset_mock()
    # Changes to the form must not change an ambiguously sent comment on retry.
    with sessions() as session:
        session.get(db.Campaign, ident).public_reply_variants = ["Changed text"]
        session.commit()
    for _ in range(2):
        page = client.post(f"/admin/safe-attempts/{aid}/retry-invitation")
        assert page.status_code == 200
    assert reply.call_args == initial and reply.call_count == 2
    assert "?reply=2042" in page.text and "Ответ отправлен" in page.text
    vk_api.send_message.assert_not_called()  # No extra invitation/promo after collecting the bonus.
    with sessions() as session:
        attempt = session.get(db.SafeAttempt, aid)
        assert attempt.public_sent and attempt.public_reply_id == 2042
        assert attempt.public_error == "" and attempt.bonus_sent
        assert session.query(db.PromoDelivery).count() == 1


def test_public_reply_precedes_dm_and_has_correct_post_and_comment(game, monkeypatch):
    client, sessions, _, instant = game
    calls = []

    async def public(comment, text, guid):
        calls.append("public")
        assert (comment.owner_id, comment.object_id, comment.comment_id) == (
            -123,
            594,
            1,
        )
        assert "vk.me" in text
        return 2042

    async def direct(*args, **kwargs):
        calls.append("dm")

    monkeypatch.setattr(vk_api, "reply_to_wall_comment", public)
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(vk_api, "send_message", direct)
    client.post("/vk/callback", json=entry(instant))
    client.post("/vk/callback", json=entry(instant))
    assert calls == ["public", "dm"]
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        assert attempt.public_sent and attempt.invitation_sent
        assert attempt.public_reply_id == 2042


def test_link_rejection_posts_plain_invitation_before_dm(game, monkeypatch):
    client, sessions, _, instant = game
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    reply = vk_api.reply_to_wall_comment
    reply.side_effect = [vk_api.VkApiError("222: Hyperlinks are forbidden"), 2042]
    client.post("/vk/callback", json=entry(instant))
    assert reply.call_count == 2
    first, second = reply.call_args_list
    assert first.kwargs["guid"] == second.kwargs["guid"]
    assert "http" in first.args[1] and "http" not in second.args[1]
    assert "Сейф" in second.args[1]
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        assert (
            attempt.public_sent and attempt.invitation_sent and not attempt.public_error
        )
        assert attempt.public_text == second.args[1]


def test_public_retry_requires_auth_csrf_and_active_valid_attempt(game):
    client, sessions, ident, instant = game
    vk_api.reply_to_wall_comment.side_effect = vk_api.VkApiError("15: denied")
    client.post("/vk/callback", json=entry(instant))
    with sessions() as session:
        aid = session.query(db.SafeAttempt).one().id
    route = f"/admin/safe-attempts/{aid}/retry-invitation"
    vk_api.reply_to_wall_comment.reset_mock()
    assert client.post(route, follow_redirects=False).status_code == 303
    login(client)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(route).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (
        client.post("/admin/safe-attempts/999999/retry-invitation").status_code == 404
    )
    with sessions() as session:
        session.get(db.Campaign, ident).enabled = False
        session.commit()
    assert client.post(route).status_code == 200
    with sessions() as session:
        session.get(db.Campaign, ident).enabled = True
        session.get(db.SafeAttempt, aid).invalid_reason = "deleted"
        session.commit()
    assert client.post(route).status_code == 200
    vk_api.reply_to_wall_comment.assert_not_called()


def test_invitation_migration_preserves_attempt_and_delivery_history(game):
    client, sessions, _, instant = game
    client.post("/vk/callback", json=entry(instant, "1234"))
    check(client, sessions)
    with db.engine.begin() as connection:
        for column in (
            "public_reply_id",
            "public_text",
            "public_error",
            "invitation_error",
        ):
            connection.execute(text(f"ALTER TABLE safe_attempts DROP COLUMN {column}"))
    db.init_db()
    db.init_db()
    with sessions() as session:
        attempt = session.query(db.SafeAttempt).one()
        assert attempt.code == "1234" and attempt.bonus_sent and attempt.public_sent
        assert attempt.public_reply_id == 0
        assert (
            attempt.public_text
            == attempt.public_error
            == attempt.invitation_error
            == ""
        )
        assert session.query(db.PromoDelivery).count() == 1


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
            db.save_settings(
                session, {"operator_user_id": str(project.group_id + 1000)}
            )
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
            assert not session.query(db.SafeAttempt).one().bonus_sent
            assert session.query(db.SafeAttempt).one().error == ""
            assert session.query(db.PromoDelivery).count() == 0
            notification = session.query(db.SafeManagerNotification).one()
            assert (
                notification.sent and notification.manager_id == project.group_id + 1000
            )
            assert project.name in notification.message
            assert f"wall-{project.group_id}_55" in notification.message
    assert all(c["method"] != "wall.getComments" for c in env.network.calls)
    for project in (first, second):
        calls = [c for c in env.network.calls if c["project_id"] == project.id]
        assert calls
        assert all(c["params"]["access_token"] == project.token for c in calls)
    # The retry route must stay in the selected project even when IDs coincide.
    with session_for(first) as session:
        attempt = session.query(db.SafeAttempt).one()
        aid = attempt.id
        attempt.public_sent, attempt.public_error = False, "15: denied"
        session.commit()
    with session_for(second) as session:
        other_reply_id = session.query(db.SafeAttempt).one().public_reply_id
    env.network.calls.clear()
    assert (
        env.client.post(
            f"/p/{first.id}/admin/safe-attempts/{aid}/retry-invitation"
        ).status_code
        == 200
    )
    assert len(env.network.calls) == 1
    assert env.network.calls[0]["project_id"] == first.id
    assert env.network.calls[0]["method"] == "wall.createComment"
    with session_for(first) as session:
        assert session.query(db.SafeAttempt).one().public_error == ""
    with session_for(second) as session:
        assert session.query(db.SafeAttempt).one().public_reply_id == other_reply_id


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
