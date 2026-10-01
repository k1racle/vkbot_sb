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
DEFAULT_CHAT_INVITATIONS = [
    "Спасибо за комментарий! Продолжим общение в чате: {chat_url}\nНажмите «Начать» или напишите «Меню».",
    "Рады вашему интересу! Поможем с выбором в сообщениях: {chat_url}\nНажмите «Начать» или отправьте «Меню».",
    "Спасибо за активность! Задайте вопрос нашему боту: {chat_url}\nДля начала диалога нажмите «Начать» или напишите «Меню».",
]


def invitation_defaults(mode):
    return DEFAULT_CHAT_INVITATIONS if mode == "chat_only" else DEFAULT_INVITATIONS


def invitation_text(campaign, values):
    variants = campaign.public_reply_variants or invitation_defaults(
        campaign.delivery_mode
    )
    return secrets.choice(variants).replace("{chat_url}", resolve_chat_url(values))


def chat_keyboard():
    return {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "Начать диалог",
                        "payload": json.dumps({"command": "start"}),
                    },
                    "color": "primary",
                }
            ]
        ],
    }


async def invite_without_gift(session, comment, campaign):
    """Invite only. Never create a PendingGift or count this as a promo delivery."""
    record = (
        session.query(ProcessedComment).filter_by(event_key=comment.event_key).one()
    )
    if (
        campaign.one_promo_per_user
        and session.query(ProcessedComment)
        .filter(
            ProcessedComment.user_id == comment.user_id,
            ProcessedComment.campaign_id == campaign.id,
            ProcessedComment.status.in_(
                ["chat_inviting", "chat_invited", "chat_invited_dm"]
            ),
            ProcessedComment.id != record.id,
        )
        .first()
    ):
        record.status = "chat_invite_duplicate"
        session.commit()
        return
    values = read_settings(session)
    invitation = invitation_text(campaign, values)
    guid = hashlib.sha256(f"chat-only:{comment.event_key}".encode()).hexdigest()[:32]
    record.status = "chat_inviting"
    session.commit()
    if comment.source_type == "wall":
        await vk_api.reply_to_wall_comment(comment, invitation, guid=guid)
    elif vk_api.video_reply_available():
        await vk_api.reply_to_video_comment(comment, invitation, guid=guid)
    else:
        from .db import Client

        client = session.get(Client, comment.user_id)
        allowed = not (
            client and (client.unsubscribed or client.messages_allowed is False)
        ) and await vk_api.is_messages_allowed(comment.user_id)
        if allowed:
            try:
                await vk_api.send_message(
                    comment.user_id,
                    invitation,
                    random_id=random_id(f"video-chat-only:{guid}"),
                    keyboard=chat_keyboard(),
                )
                record.status, record.error = "chat_invited_dm", None
                session.commit()
                return
            except vk_api.VkApiError as error:
                if error.code not in {901, 902}:
                    raise
        record.status = "chat_invite_unavailable"
        record.error = (
            "Приглашение не отправлено: нет токена для ответа под видео и разрешения "
            "на личные сообщения. Подключите токен в «Проектах» или разместите ссылку "
            "на чат в описании видео: " + resolve_chat_url(values)
        )
        session.commit()
        return
    record.status, record.error = "chat_invited", None
    session.commit()


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
    active_key = f"{comment.user_id}:{campaign.id}"
    gift = session.query(PendingGift).filter_by(active_key=active_key).first()
    if gift is not None:
        previous = (
            session.query(ProcessedComment).filter_by(event_key=gift.event_key).first()
        )
        # Only a definite authentication rejection proves that VK did not send
        # the invitation. Timeouts and ambiguous failures must remain deduplicated.
        rejected = (
            previous is not None
            and previous.status == "failed"
            and (previous.error or "").partition(":")[0] == "5"
        )
        unavailable = previous is not None and previous.status == "video_waiting_chat"
        can_reply = comment.source_type == "wall" or vk_api.video_reply_available()
        if gift.status != "pending" or not can_reply or not (rejected or unavailable):
            record.status = "invite_duplicate"
            session.commit()
            return
        # Keep the same gift, message and guid. Point to the new attempt before
        # sending so a subsequent comment cannot retry an already successful or
        # interrupted attempt using the original authentication error.
        gift.event_key = comment.event_key
        invitation = gift.invitation_text
    else:
        invitation = invitation_text(campaign, read_settings(session))
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
    if comment.source_type == "wall":
        await vk_api.reply_to_wall_comment(comment, invitation, guid=gift.id)
    elif vk_api.video_reply_available():
        await vk_api.reply_to_video_comment(comment, invitation, guid=gift.id)
    else:
        # No personal credential: try only a permitted community DM, never
        # bypass permission 901. Retain the gift even when neither path works.
        from .db import Client

        client = session.get(Client, comment.user_id)
        allowed = not (
            client and (client.unsubscribed or client.messages_allowed is False)
        ) and await vk_api.is_messages_allowed(comment.user_id)
        if allowed:
            try:
                await vk_api.send_message(
                    comment.user_id,
                    invitation,
                    random_id=random_id(f"video-invite:{gift.id}"),
                    keyboard=gift_keyboard("Получить подарок"),
                )
                record.status = "video_invited_dm"
                session.commit()
                return
            except vk_api.VkApiError as error:
                if error.code not in {901, 902}:
                    raise
        record.status = "video_waiting_chat"
        record.error = (
            "Подарок сохранён, приглашение не отправлено: нет токена для ответа под видео "
            "и разрешения на личные сообщения. Подключите токен в «Проектах» или "
            "разместите ссылку на чат в описании видео: "
            + resolve_chat_url(read_settings(session))
        )
        session.commit()


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
    if gift is None:
        if not explicit:
            return False  # Ordinary Start still starts the configured dialog.
        # Gift commands belong only to projects that actually offer gifts.
        # A chat-only project must use its own scenarios/fallback, even if a
        # customer types an old command or clicks a previously sent gift button.
        offers_gifts = (
            session.query(Campaign.id)
            .filter(
                Campaign.enabled.is_(True),
                Campaign.is_deleted.is_(False),
                Campaign.delivery_mode.in_(("direct", "chat_invite")),
            )
            .first()
        )
        if offers_gifts is None:
            return False
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
                "Если подарок уже получен, промокод есть выше в переписке."
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
            if (
                not campaign
                or campaign.is_deleted
                or not campaign.enabled
                or campaign.delivery_mode == "chat_only"
            ):
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
