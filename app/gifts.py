"""Comment invitations and durable, user-bound gift claims.

Callers hold the shared customer lock (and a PostgreSQL advisory transaction
lock). Opening the public link is not consent: a private gift request is required
before delivery, including automatic delivery after joining the community.
No identifiers supplied by the client select another user.
"""

import hashlib
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from . import vk_api
from .config import get_settings
from .db import (
    Campaign,
    Conversation,
    PendingGift,
    ProcessedComment,
    PromoDelivery,
    read_settings,
)
from .flows import render

logger = logging.getLogger(__name__)
DEFAULT_INVITATIONS = [
    (
        "Спасибо за комментарий и активность 💚 Мы приготовили для вас подарок! "
        "Заберите его в сообщениях: {chat_url}\n"
        "Нажмите «Начать» или напишите «Подарок»."
    ),
    (
        "Спасибо, что делитесь впечатлениями! 🎁 Ваш подарок ждёт в чате "
        "сообщества: {chat_url}\nНажмите «Начать», а если кнопки нет — отправьте «Подарок»."
    ),
    (
        "Рады вашему комментарию 💚 Хотим порадовать вас подарком: {chat_url}\n"
        "Перейдите в чат и нажмите «Начать» или напишите «Подарок»."
    ),
    (
        "Благодарим за участие! Для вас есть приятный бонус ✨ "
        "Получить его можно здесь: {chat_url}\n"
        "В чате нажмите «Начать» или отправьте слово «Подарок»."
    ),
]


def validate_chat_url(value: str) -> str:
    """Keep the admin's HTTPS destination intact; blank means automatic URL."""
    value = value.strip()
    if not value:
        return ""
    if len(value) > 500 or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '\\<>"{}' for c in value
    ):
        raise ValueError("Invalid chat URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port == 0
    ):
        raise ValueError("Invalid chat URL")
    return value


def resolve_chat_url(values: dict[str, str]) -> str:
    # Also tolerate a bad manually edited/legacy value without posting an unsafe link.
    try:
        custom = validate_chat_url(values.get("chat_url", ""))
    except ValueError:
        custom = ""
    return custom or f"https://vk.me/club{get_settings().vk_group_id}"


def delivered(session, user_id, campaign):
    return (
        session.query(PromoDelivery)
        .filter_by(user_id=user_id, campaign_id=campaign.id)
        .first()
        is not None
        or session.query(ProcessedComment)
        .filter_by(user_id=user_id, campaign_id=campaign.id, status="sent")
        .first()
        is not None
    )


def random_id(key):
    return (
        int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") & 0x7FFFFFFF
        or 1
    )


def gift_keyboard(label):
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": label,
                        "payload": json.dumps({"action": "claim_gift"}),
                    },
                    "color": "positive",
                }
            ]
        ],
    }


def subscription_keyboard():
    keyboard = gift_keyboard("Проверить подписку")
    keyboard["buttons"].insert(
        0,
        [
            {
                "action": {
                    "type": "open_link",
                    "label": "Подписаться на сообщество",
                    "link": f"https://vk.ru/club{get_settings().vk_group_id}",
                },
            }
        ],
    )
    return keyboard


async def invite_to_chat(session, comment, campaign):
    record = (
        session.query(ProcessedComment).filter_by(event_key=comment.event_key).one()
    )
    if comment.source_type != "wall":
        record.status = "invite_unsupported"
        record.error = "Приглашение под видео недоступно с ключом сообщества. Используйте прямую отправку."
        session.commit()
        return
    active_key = f"{comment.user_id}:{campaign.id}"
    if session.query(PendingGift).filter_by(active_key=active_key).first():
        record.status = "invite_duplicate"
        session.commit()
        return
    variants = campaign.public_reply_variants or DEFAULT_INVITATIONS
    chat_url = resolve_chat_url(read_settings(session))
    invitation = secrets.choice(variants).replace("{chat_url}", chat_url)
    gift = PendingGift(
        id=uuid4().hex,
        user_id=comment.user_id,
        campaign_id=campaign.id,
        event_key=comment.event_key,
        active_key=active_key,
        invitation_text=invitation,
    )
    session.add(gift)
    record.status = "waiting_chat"
    # A failed/ambiguous public reply must not erase the customer's entitlement.
    # Duplicate callbacks never create another invite; wall guid is stable too.
    session.commit()
    await vk_api.reply_to_wall_comment(comment, invitation, guid=gift.id)


async def handle_gift_request(session, event, message, incoming, *, automatic=False):
    word = str(message.get("text", "")).strip().casefold()
    explicit = incoming.get("action") == "claim_gift" or (
        not incoming and word in {"подарок", "получить подарок", "/gift", "🎁 подарок"}
    )
    start = (not incoming and word in {"начать", "старт", "/start"}) or incoming == {
        "command": "start"
    }
    if not (explicit or start or event.kind == "gift"):
        return False
    # A failed incoming event remains bound to its original gift even if another
    # message has since claimed it. Retrying it must never issue the next gift.
    if event.gift_id:
        gift = session.get(PendingGift, event.gift_id)
        if gift is None or gift.user_id != event.user_id or gift.status != "pending":
            event.status = "done"
            session.commit()
            return True
    else:
        gift = (
            session.query(PendingGift)
            .filter_by(user_id=event.user_id, status="pending")
            .order_by(PendingGift.created_at, PendingGift.id)
            .first()
        )
    if gift is None and not explicit:
        return False  # Ordinary Start still starts the configured dialog.
    event.kind = "gift_join" if automatic else "gift"
    event.text = str(message.get("text", ""))[:4000]
    event.gift_id = gift.id if gift else None
    session.add(event)
    if session.get(Conversation, event.user_id) is None:
        session.add(Conversation(user_id=event.user_id, variables={}))
    record = (
        session.query(ProcessedComment).filter_by(event_key=gift.event_key).first()
        if gift
        else None
    )

    async def notice(text, keyboard=None):
        if automatic:
            return  # Join events must not generate unsolicited reminders.
        await vk_api.send_message(
            event.user_id,
            text,
            random_id=random_id(f"gift-notice:{event.event_key}"),
            keyboard=keyboard,
        )

    try:
        if automatic and (gift is None or not gift.awaiting_subscription):
            event.status = "done"
            session.commit()
            return True
        if gift is None:
            await notice(
                "Пока нет подарков к получению. Оставьте подходящий комментарий под постом акции. "
                "Если подарок уже получен, промокод есть выше в переписке. Для обычного меню напишите «меню»."
            )
        else:
            campaign = session.get(Campaign, gift.campaign_id)
            more = (
                session.query(PendingGift)
                .filter(
                    PendingGift.user_id == event.user_id,
                    PendingGift.status == "pending",
                    PendingGift.id != gift.id,
                )
                .first()
                is not None
            )
            keyboard = gift_keyboard("Следующий подарок") if more else None
            if not campaign or not campaign.enabled:
                gift.status, gift.active_key = "cancelled", None
                gift.awaiting_subscription = False
                if record:
                    record.status = "gift_unavailable"
                await notice(
                    "Эта акция сейчас недоступна. Для связи с нами напишите «меню».",
                    keyboard,
                )
            elif campaign.one_promo_per_user and delivered(
                session, event.user_id, campaign
            ):
                gift.status, gift.active_key = "cancelled", None
                gift.awaiting_subscription = False
                if record:
                    record.status = "already_sent"
                await notice(
                    "Вы уже получили промокод этой акции. Посмотрите выше в переписке.",
                    keyboard,
                )
            elif not await vk_api.is_group_member(event.user_id):
                gift.awaiting_subscription = True
                if record:
                    record.status, record.error = "waiting_subscription", None
                await notice(
                    "Ваш подарок уже ждёт 🎁 Чтобы получить его, подпишитесь на сообщество: "
                    f"https://vk.ru/club{get_settings().vk_group_id}\n"
                    "Когда VK сообщит нам о подписке, я отправлю промокод автоматически. "
                    "Если сообщение задержится, нажмите «Проверить подписку».",
                    subscription_keyboard(),
                )
            elif automatic and not await vk_api.is_messages_allowed(event.user_id):
                # Consent can be revoked between Start and joining. Do not try
                # to bypass it or consume the pending gift on this event.
                event.status, event.error = (
                    "waiting_permission",
                    "Сообщения сообщества не разрешены",
                )
                if record:
                    record.status, record.error = "waiting_permission", event.error
                session.commit()
                return True
            else:
                # Once a send was attempted, keep the same content and random_id
                # on retries, including retries triggered by a new user message.
                if not gift.delivery_payload:
                    name = await vk_api.get_user_name(event.user_id)
                    attachment = ""
                    if campaign.attachment_path:
                        attachment = await vk_api.upload_file_for_message(
                            event.user_id,
                            Path(campaign.attachment_path),
                            campaign.attachment_name,
                            campaign.attachment_type,
                        )
                    gift.delivery_payload = {
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
                    }
                await vk_api.send_message(
                    event.user_id,
                    gift.delivery_payload["text"],
                    random_id=random_id(f"gift:{gift.id}"),
                    attachment=gift.delivery_payload["attachment"],
                    keyboard=keyboard,
                )
                gift.status, gift.active_key, gift.error = "sent", None, ""
                gift.awaiting_subscription = False
                session.add(
                    PromoDelivery(user_id=event.user_id, campaign_id=campaign.id)
                )
                if record:
                    record.status, record.error = "sent", None
        event.status, event.error = "done", ""
        session.commit()
        return True
    except (vk_api.VkApiError, httpx.HTTPError, OSError) as error:
        # Persist the payload and event binding before VK retries the callback.
        # A customer can also retry by sending «Подарок» again.
        if gift:
            gift.error = str(error)[:1000]
            if record:
                record.status, record.error = "gift_failed", gift.error
        event.status, event.error = "failed", str(error)[:1000]
        session.commit()
        logger.warning("Gift claim failed for user %s: %s", event.user_id, error)
        raise
