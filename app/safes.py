"""One original attempt per account; one durable prize per project campaign.

The button reveals the saved code. Authenticated comment callbacks supply the
chronology before an atomic winner claim. No database lock spans network I/O.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update

from . import db, vk_api
from .comments import integer, normalize_comment
from .config import get_settings
from .flows import render
from .gifts import random_id

DEFAULT_MESSAGES = {
    "invitation": "Твоя попытка зарегистрирована! Нажми «Проверить код», чтобы узнать результат 🔐",
    "wrong": "Упс! Сейф пока не поддался 🔐\nНо мы не любим отпускать гостей без подарков!",
    "winner": "ДА ЛАДНО?! 😱\nТЫ ВЗЛОМАЛ НАШ СЕЙФ!\nПоздравляем! Код подошёл, и бокс твой! 🎁",
    "later": "Код верный, но сейф уже открыли раньше 🔐\nДля тебя тоже есть подарок!",
}
DEFAULT_INVITATIONS = [
    "🔐 Комбинация принята! Проверь код в сообщениях сообщества: {chat_url}\nНажми «Начать» или напиши «Сейф» 🎁"
]
MOSCOW = timezone(timedelta(hours=3))


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def timestamp(value):
    return int(value.replace(tzinfo=timezone.utc).timestamp())


def local_date(value):
    return (
        value.replace(tzinfo=timezone.utc).astimezone(MOSCOW).strftime("%Y-%m-%dT%H:%M")
        if value
        else ""
    )


def display_time(value):
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .astimezone(MOSCOW)
        .strftime("%d.%m.%Y %H:%M:%S")
    )


def parse_date(value):
    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
        .replace(tzinfo=MOSCOW)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def valid_code(value):
    return bool(re.fullmatch(r"[0-9]{4}", value))


def test_mode(session):
    value = db.read_settings(session).get("test_mode", get_settings().test_mode)
    return str(value).lower() in {"true", "1", "yes", "on"}


def messages(campaign):
    return DEFAULT_MESSAGES | (campaign.safe_messages or {})


def keyboard(attempt_id, subscribe=False):
    result = {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "Проверить код",
                        "payload": json.dumps(
                            {"action": "safe_check", "attempt_id": attempt_id}
                        ),
                    },
                    "color": "primary",
                }
            ]
        ],
    }
    if subscribe:
        result["buttons"].insert(
            0,
            [
                {
                    "action": {
                        "type": "open_link",
                        "label": "Подписаться",
                        "link": f"https://vk.ru/club{get_settings().vk_group_id}",
                    }
                }
            ],
        )
    return result


def in_window(campaign, published_at):
    return bool(
        campaign.safe_start_at
        and campaign.safe_end_at
        and timestamp(campaign.safe_start_at)
        <= published_at
        < timestamp(campaign.safe_end_at)
    )


def register_comment(payload):
    """Consume authenticated wall events synchronously; return a new invite ID.

    All original comments are retained, including non-codes. A later edit into
    a valid code cannot invent an original entry when checking a saved attempt.
    """
    kind = payload.get("type")
    if kind not in {
        "wall_reply_new",
        "wall_reply_edit",
        "wall_reply_delete",
        "wall_reply_restore",
    }:
        return False, None
    obj = payload.get("object")
    if not isinstance(obj, dict):
        return False, None
    owner = integer(
        obj.get("post_owner_id", obj.get("owner_id", -get_settings().vk_group_id))
    )
    post_id, comment_id = integer(obj.get("post_id")), integer(obj.get("id"))
    if owner != -get_settings().vk_group_id or min(post_id, comment_id) <= 0:
        return False, None
    with db.SessionLocal() as session:
        campaign = (
            session.query(db.Campaign)
            .filter_by(post_id=post_id, is_deleted=False, delivery_mode="safe")
            .with_for_update()
            .first()
        )
        if campaign is None:
            return False, None
        # Even on campaign pause, retain edit/delete evidence. New entries also
        # remain recorded, but cannot trigger invitations while paused.
        row = (
            session.query(db.SafeComment)
            .filter_by(campaign_id=campaign.id, comment_id=comment_id)
            .first()
        )
        if row is None:
            row = db.SafeComment(campaign_id=campaign.id, comment_id=comment_id)
            session.add(row)
            session.flush()
        attempt = None
        invite_id = None
        if kind == "wall_reply_new":
            comment = normalize_comment(payload, get_settings().vk_group_id)
            if comment is None or not comment.published_at:
                session.rollback()
                return True, None
            if row.original_seen:
                # A redelivered callback may retry delivery, never participation.
                attempt = (
                    session.query(db.SafeAttempt)
                    .filter_by(campaign_id=campaign.id, user_id=comment.user_id)
                    .first()
                )
                return (
                    True,
                    attempt.id
                    if attempt and attempt.comment_id == comment_id and campaign.enabled
                    else None,
                )
            row.user_id, row.published_at = comment.user_id, comment.published_at
            row.original_text, row.original_seen = comment.text, True
            campaign.safe_revision = db.Campaign.safe_revision + 1
            event = db.ProcessedComment(
                event_key=comment.event_key,
                source_type="wall",
                owner_id=owner,
                comment_id=comment_id,
                post_id=post_id,
                user_id=comment.user_id,
                campaign_id=campaign.id,
                status="safe_invalid_format",
            )
            if (
                session.query(db.ProcessedComment)
                .filter_by(event_key=comment.event_key)
                .first()
                is None
            ):
                session.add(event)
            else:
                event = (
                    session.query(db.ProcessedComment)
                    .filter_by(event_key=comment.event_key)
                    .one()
                )
            if not in_window(campaign, comment.published_at):
                event.status = "safe_outside_dates"
            elif not campaign.safe_locked:
                event.status = "safe_not_started"
            elif valid_code(comment.text):
                attempt = (
                    session.query(db.SafeAttempt)
                    .filter_by(campaign_id=campaign.id, user_id=comment.user_id)
                    .first()
                )
                if attempt is None:
                    from .clients import ensure_client

                    ensure_client(session, comment.user_id)
                    attempt = db.SafeAttempt(
                        campaign_id=campaign.id,
                        user_id=comment.user_id,
                        comment_id=comment_id,
                        published_at=comment.published_at,
                        code=comment.text,
                        invalid_reason=row.invalid_reason,
                    )
                    session.add(attempt)
                    session.flush()
                    invite_id = attempt.id
                    event.status = "safe_registered"
                elif campaign.safe_winner_id != attempt.id and (
                    comment.published_at,
                    comment_id,
                ) < (attempt.published_at, attempt.comment_id):
                    # Callback delivery order is not publication order.
                    attempt.comment_id, attempt.published_at, attempt.code = (
                        comment_id,
                        comment.published_at,
                        comment.text,
                    )
                    attempt.invalid_reason = row.invalid_reason
                    event.status = "safe_first_corrected"
                else:
                    event.status = "safe_duplicate"
        else:
            reason = "edited" if kind == "wall_reply_edit" else "deleted"
            if not row.invalid_reason:
                row.invalid_reason = reason
                campaign.safe_revision = db.Campaign.safe_revision + 1
            attempt = (
                session.query(db.SafeAttempt)
                .filter_by(campaign_id=campaign.id, comment_id=comment_id)
                .first()
            )
            if attempt and campaign.safe_winner_id != attempt.id:
                attempt.invalid_reason = row.invalid_reason
        session.commit()
        return True, invite_id if campaign.enabled else None


async def send_invitation(attempt_id):
    from .comments import Comment
    from .gifts import invitation_text

    with db.SessionLocal() as session:
        attempt = session.get(db.SafeAttempt, attempt_id)
        campaign = session.get(db.Campaign, attempt.campaign_id)
        if (
            not campaign.enabled
            or campaign.is_deleted
            or attempt.invalid_reason
            or test_mode(session)
        ):
            return
        values = db.read_settings(session)
        client = session.get(db.Client, attempt.user_id)
        if client and client.unsubscribed:
            return
        comment = Comment(
            "wall",
            -get_settings().vk_group_id,
            campaign.post_id,
            attempt.comment_id,
            attempt.user_id,
            attempt.code,
        )
        errors = []
        if not attempt.public_sent:
            try:
                await vk_api.reply_to_wall_comment(
                    comment,
                    invitation_text(campaign, values),
                    guid=f"safe-{campaign.id}-{attempt.id}",
                )
                attempt.public_sent = True
                session.commit()
            except (vk_api.VkApiError, httpx.HTTPError) as error:
                errors.append(str(error))
        if not attempt.invitation_sent and not (
            client and client.messages_allowed is False
        ):
            try:
                if await vk_api.is_messages_allowed(attempt.user_id):
                    await vk_api.send_message(
                        attempt.user_id,
                        messages(campaign)["invitation"],
                        random_id=random_id(f"safe-invite:{attempt.id}"),
                        keyboard=keyboard(attempt.id),
                    )
                    attempt.invitation_sent = True
            except (vk_api.VkApiError, httpx.HTTPError) as error:
                errors.append(str(error))
        attempt.error = "\n".join(errors)[:1000]
        session.commit()


class VerificationPending(ValueError):
    pass


async def claim_winner(attempt_id):
    """Return winner/earlier/later/invalid. Membership and API errors fail closed."""
    with db.SessionLocal() as session:
        attempt = session.get(db.SafeAttempt, attempt_id)
        campaign = session.get(db.Campaign, attempt.campaign_id)
        if campaign.safe_winner_id:
            return "winner" if campaign.safe_winner_id == attempt_id else "later"
        revision = campaign.safe_revision
        evidence = {
            r.comment_id: r
            for r in session.query(db.SafeComment).filter_by(campaign_id=campaign.id)
        }
        attempts = (
            session.query(db.SafeAttempt)
            .filter_by(campaign_id=campaign.id)
            .order_by(db.SafeAttempt.published_at, db.SafeAttempt.comment_id)
            .all()
        )
    cutoff = (attempt.published_at, attempt.comment_id)
    invalid = {}
    eligible = []
    for entry in attempts:
        if (entry.published_at, entry.comment_id) > cutoff:
            break
        original = evidence.get(entry.comment_id)
        if original is None or not original.original_seen:
            raise VerificationPending(
                "Не найдено исходное событие сохранённой попытки. Проверьте историю кампании."
            )
        if (
            original.original_text.strip() != entry.code
            or original.user_id != entry.user_id
            or original.published_at != entry.published_at
            or not in_window(campaign, entry.published_at)
        ):
            raise VerificationPending(
                "Сохранённая попытка не совпадает с исходным комментарием. Проверьте историю кампании."
            )
        reason = entry.invalid_reason or original.invalid_reason
        if reason:
            invalid[entry.id] = reason
        elif entry.code == campaign.safe_code:
            eligible.append(entry)
    first = None
    for entry in eligible:
        if await vk_api.is_group_member(entry.user_id):
            first = entry.id
            break
    with db.SessionLocal() as session:
        # A callback, pause, settings change or another winner invalidates this
        # saved history. The conditional write also arbitrates multiple workers.
        changed = session.execute(
            update(db.Campaign)
            .where(
                db.Campaign.id == campaign.id,
                db.Campaign.safe_revision == revision,
                db.Campaign.safe_winner_id.is_(None),
                db.Campaign.enabled.is_(True),
                db.Campaign.is_deleted.is_(False),
            )
            .values(
                safe_revision=revision + 1,
                **(
                    {"safe_winner_id": attempt_id, "safe_finalized_at": now()}
                    if first == attempt_id
                    else {}
                ),
            )
        )
        if changed.rowcount != 1:
            session.rollback()
            fresh = session.get(db.Campaign, campaign.id)
            if fresh.safe_winner_id:
                return "winner" if fresh.safe_winner_id == attempt_id else "later"
            raise VerificationPending(
                "Комментарии обновились во время проверки. Нажмите «Проверить код» ещё раз."
            )
        for ident, reason in invalid.items():
            session.get(db.SafeAttempt, ident).invalid_reason = reason
        session.commit()
    if attempt_id in invalid:
        return "invalid"
    return "winner" if first == attempt_id else "earlier" if first else "not_member"


async def handle_request(session, event, message, incoming):
    word = str(message.get("text", "")).strip().casefold()
    check = incoming.get("action") == "safe_check"
    start = incoming == {"command": "start"} or (
        not incoming and word in {"начать", "старт", "/start"}
    )
    explicit = not incoming and word in {
        "сейф",
        "проверить код",
        "узнать результат",
        "/safe",
    }
    if not (check or start or explicit or event.safe_attempt_id):
        return False
    ident = event.safe_attempt_id or (
        integer(incoming.get("attempt_id")) if check else 0
    )
    query = (
        session.query(db.SafeAttempt)
        .join(db.Campaign, db.Campaign.id == db.SafeAttempt.campaign_id)
        .filter(
            db.SafeAttempt.user_id == event.user_id,
            db.Campaign.is_deleted.is_(False),
            db.Campaign.delivery_mode == "safe",
        )
    )
    attempt = (
        query.filter(db.SafeAttempt.id == ident).first()
        if ident
        else query.filter(db.Campaign.enabled.is_(True))
        .order_by(db.SafeAttempt.bonus_sent, db.SafeAttempt.id.desc())
        .first()
    )
    if attempt is None and not check:
        return False
    if start and attempt and attempt.bonus_sent:
        return False  # Ordinary Start is available again after collecting the bonus.
    event.kind, event.text = "safe", str(message.get("text", ""))[:4000]
    session.add(event)
    if attempt:
        event.safe_attempt_id = attempt.id
    session.commit()

    async def notice(text, buttons=None, key=None):
        await vk_api.send_message(
            event.user_id,
            text,
            random_id=random_id(key or f"safe-event:{event.event_key}"),
            keyboard=buttons,
        )

    try:
        if attempt is None:
            await notice("Попытка не найдена. Оставьте свой код под постом игры.")
        else:
            campaign = session.get(db.Campaign, attempt.campaign_id)
            if test_mode(session):
                await notice(
                    "Сейф временно недоступен: включён тестовый режим. Попытка сохранена."
                )
            elif not campaign.enabled or campaign.is_deleted:
                await notice("Эта игра сейчас приостановлена. Попытка сохранена.")
            elif start or (explicit and word in {"сейф", "/safe"}):
                await notice(messages(campaign)["invitation"], keyboard(attempt.id))
            elif attempt.invalid_reason and campaign.safe_winner_id != attempt.id:
                await notice(
                    "Попытка не участвует: первый комментарий изменён или удалён. Новая попытка не предоставляется."
                )
            elif not await vk_api.is_group_member(event.user_id):
                await notice(
                    "Чтобы проверить код и получить подарок, подпишитесь на сообщество, затем нажмите «Проверить код».",
                    keyboard(attempt.id, subscribe=True),
                )
            else:
                outcome = "wrong"
                if attempt.code == campaign.safe_code:
                    # End our read transaction before checking with independent sessions.
                    session.commit()
                    try:
                        outcome = await claim_winner(attempt.id)
                    except (vk_api.VkApiError, httpx.HTTPError) as error:
                        raise VerificationPending(str(error)) from error
                    session.expire_all()
                    campaign = session.get(db.Campaign, attempt.campaign_id)
                if outcome == "invalid":
                    await notice(
                        "Попытка не участвует: первый комментарий изменён или удалён. Новая попытка не предоставляется."
                    )
                elif outcome == "not_member":
                    await notice(
                        "Подписка не подтвердилась. Подпишитесь и проверьте код ещё раз.",
                        keyboard(attempt.id, subscribe=True),
                    )
                else:
                    text = (
                        "Код подошёл, но есть более ранний правильный комментарий участника. Главный приз пока не подтверждён. Для тебя есть бонус 🎁"
                        if outcome == "earlier"
                        else messages(campaign)[outcome]
                    )
                    if outcome == "winner" and campaign.safe_notification_sent:
                        text += "\nПобеда уже закреплена за вами. Повторное нажатие не создаёт новый приз."
                    await notice(
                        text,
                        key=f"safe-win:{campaign.id}"
                        if outcome == "winner" and not campaign.safe_notification_sent
                        else None,
                    )
                    if outcome == "winner":
                        (
                            campaign.safe_notification_sent,
                            campaign.safe_notification_error,
                        ) = True, ""
                        session.commit()
                    await send_bonus(session, attempt, campaign)
        event.status, event.error = "done", ""
        if attempt:
            attempt.error = ""
        session.commit()
        return True
    except VerificationPending as error:
        await notice(
            "Пока не удалось завершить проверку комментариев. Попробуйте чуть позже — нажмите «Проверить код» 🔐",
            keyboard(attempt.id),
        )
        event.status, event.error = "done", str(error)[:1000]
        attempt.error = event.error
        session.commit()
        return True
    except (vk_api.VkApiError, httpx.HTTPError, OSError) as error:
        event.status, event.error = "failed", str(error)[:1000]
        if attempt:
            attempt.error = event.error
            session.refresh(campaign)
            if (
                campaign.safe_winner_id == attempt.id
                and not campaign.safe_notification_sent
            ):
                campaign.safe_notification_error = event.error
        session.commit()
        raise


async def send_bonus(session, attempt, campaign):
    if attempt.bonus_sent:
        return
    if not attempt.bonus_payload:
        from pathlib import Path

        name = await vk_api.get_user_name(attempt.user_id)
        attachment = ""
        if campaign.attachment_path:
            attachment = await vk_api.upload_file_for_message(
                attempt.user_id,
                Path(campaign.attachment_path),
                campaign.attachment_name,
                campaign.attachment_type,
            )
        payload = {
            "text": render(
                campaign.promo_message,
                {
                    "first_name": name,
                    "user_name": name,
                    "promo_code": campaign.promo_code,
                    "shop_url": campaign.shop_url,
                },
            ),
            "attachment": attachment,
            "promo_code": campaign.promo_code,
        }
        session.commit()
        locked = (
            session.query(db.SafeAttempt)
            .filter_by(id=attempt.id)
            .with_for_update()
            .populate_existing()
            .one()
        )
        if not locked.bonus_payload:
            locked.bonus_payload = payload
        session.commit()  # Persist exact content before an ambiguous external send.
    await vk_api.send_message(
        attempt.user_id,
        attempt.bonus_payload["text"],
        random_id=random_id(f"safe-bonus:{attempt.id}"),
        attachment=attempt.bonus_payload["attachment"],
    )
    changed = session.execute(
        update(db.SafeAttempt)
        .where(db.SafeAttempt.id == attempt.id, db.SafeAttempt.bonus_sent.is_(False))
        .values(bonus_sent=True, error="")
    )
    if changed.rowcount:
        session.add(
            db.PromoDelivery(
                user_id=attempt.user_id,
                campaign_id=campaign.id,
                promo_code=attempt.bonus_payload["promo_code"],
            )
        )
    session.commit()
