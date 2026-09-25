import asyncio
import hashlib
import json
import logging
import weakref
from pathlib import Path

from sqlalchemy import text as sql_text

from . import vk_api
from .config import get_settings
from .db import (
    Campaign,
    Conversation,
    DialogEvent,
    MediaAsset,
    ProcessedComment,
    PromoDelivery,
    Scenario,
    SessionLocal,
    read_settings,
)
from .flows import advance, matches, render

logger = logging.getLogger(__name__)
_locks = weakref.WeakValueDictionary()
# Bound open synchronous DB transactions while VK requests are in flight.
CALLBACK_SLOTS = asyncio.Semaphore(4)


def user_lock(user_id):
    return _locks.setdefault(user_id, asyncio.Lock())


def truth(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def delivered(session, user_id, campaign):
    return (
        session.query(PromoDelivery)
        .filter_by(user_id=user_id, campaign_id=campaign.id)
        .first()
        is not None
        or session.query(ProcessedComment)
        .filter_by(user_id=user_id, post_id=campaign.post_id, status="sent")
        .first()
        is not None
    )


class LivePort:
    def __init__(self, session, user_id, event_key, values):
        self.session, self.user_id, self.event_key, self.values = (
            session,
            user_id,
            event_key,
            values,
        )
        self.counter = 0
        self.warning = ""

    def nonce(self, step):
        return hashlib.sha256(f"{self.event_key}:{step}".encode()).hexdigest()[:16]

    async def emit(
        self, message, keyboard=None, media_id="", attachment="", recipient=None
    ):
        if keyboard is None:
            keyboard = {"one_time": False, "buttons": []}
        if media_id:
            asset = self.session.get(MediaAsset, media_id)
            if asset is None or not Path(asset.path).is_file():
                raise ValueError("Файл сценария не найден")
            attachment = await vk_api.upload_file_for_message(
                self.user_id, Path(asset.path), asset.filename, asset.content_type
            )
        self.counter += 1
        random_id = (
            int.from_bytes(
                hashlib.sha256(f"{self.event_key}:{self.counter}".encode()).digest()[
                    :4
                ],
                "big",
            )
            & 0x7FFFFFFF
        )
        await vk_api.send_message(
            recipient or self.user_id,
            message,
            random_id=random_id or 1,
            attachment=attachment,
            keyboard=keyboard,
        )

    async def check(self, node):
        if node["condition"] == "member":
            return await vk_api.is_group_member(self.user_id)
        campaign = self.session.get(Campaign, node["campaign_id"])
        return bool(campaign and delivered(self.session, self.user_id, campaign))

    async def promo(self, campaign_id, variables):
        campaign = self.session.get(Campaign, campaign_id)
        if not campaign or not campaign.enabled:
            await self.emit("Эта акция сейчас недоступна.")
            return
        if not await vk_api.is_group_member(self.user_id):
            await self.emit(
                "Подпишитесь на сообщество, чтобы получить промокод. Затем попробуйте снова через меню."
            )
            return
        if campaign.one_promo_per_user and delivered(
            self.session, self.user_id, campaign
        ):
            await self.emit(
                "Вы уже получали промокод этой акции. Он есть выше в переписке."
            )
            return
        variables.update(promo_code=campaign.promo_code, shop_url=campaign.shop_url)
        attachment = ""
        if campaign.attachment_path:
            attachment = await vk_api.upload_file_for_message(
                self.user_id,
                Path(campaign.attachment_path),
                campaign.attachment_name,
                campaign.attachment_type,
            )
        await self.emit(
            render(campaign.promo_message, variables), attachment=attachment
        )
        self.session.add(PromoDelivery(user_id=self.user_id, campaign_id=campaign_id))
        self.session.flush()

    async def handoff(self, message):
        settings = get_settings()
        operator = str(
            self.values.get("operator_user_id", settings.operator_user_id)
        ).strip()
        if not operator.isdigit() or int(operator) <= 0:
            await self.emit(
                "Напишите ваш вопрос здесь. Он останется в сообщениях сообщества для менеджера."
            )
            return
        await self.emit(
            message or self.values.get("operator_ack") or settings.operator_ack,
            keyboard={"one_time": False, "buttons": []},
        )
        conversation = self.session.get(Conversation, self.user_id)
        details = "\n".join(
            f"{k}: {v}"
            for k, v in conversation.variables.items()
            if k not in {"first_name", "user_name"} and not k.startswith("_")
        )
        try:
            await self.emit(
                f"Нужен менеджер: https://vk.com/id{self.user_id}\n{details}"[:4000],
                recipient=int(operator),
            )
        except vk_api.VkApiError as error:
            # The client must stay handed off even if the manager forbids DMs.
            self.warning = f"Уведомление менеджеру не доставлено: {error}"
            logger.warning("Operator notification failed for user %s", self.user_id)


async def handle_message(payload):
    obj = payload.get("object") or {}
    message = obj.get("message", obj)
    user_id = int(message.get("from_id", 0))
    if (
        user_id <= 0
        or message.get("out")
        or int(message.get("peer_id", user_id)) != user_id
    ):
        return
    event_id = (
        payload.get("event_id")
        or message.get("conversation_message_id")
        or message.get("id")
    )
    if not event_id:
        return
    key = f"{payload.get('group_id', 0)}:{user_id}:{event_id}"[:160]
    lock = user_lock(user_id)
    async with lock, CALLBACK_SLOTS:
        with SessionLocal() as session:
            # Serialize messages of one client across app processes on PostgreSQL too.
            if session.bind.dialect.name == "postgresql":
                session.execute(
                    sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_id}
                )
            event = session.query(DialogEvent).filter_by(event_key=key).first()
            if event and event.status == "done":
                return
            values = read_settings(session)
            if not truth(values.get("chat_enabled") or get_settings().chat_enabled):
                return
            conversation = session.get(Conversation, user_id)
            if conversation is None:
                conversation = Conversation(user_id=user_id, variables={})
                session.add(conversation)
                session.flush()
            event = event or DialogEvent(event_key=key, user_id=user_id)
            event.text = str(message.get("text", ""))[:4000]
            session.add(event)
            conversation.variables = dict(
                conversation.variables, last_message=event.text
            )
            port = LivePort(session, user_id, key, values)
            try:
                incoming = message.get("payload") or {}
                if isinstance(incoming, str):
                    try:
                        incoming = json.loads(incoming)
                    except (ValueError, TypeError):
                        incoming = {}
                if not isinstance(incoming, dict):
                    incoming = {}
                restart = not incoming and event.text.strip().casefold() in {
                    "меню",
                    "начать",
                    "старт",
                    "/start",
                }
                if conversation.handoff and not restart:
                    event.status = "done"
                    session.commit()
                    return
                if restart:
                    conversation.handoff = False
                triggers = (
                    values.get("operator_trigger_words")
                    or get_settings().operator_trigger_words
                )
                if not incoming and matches(event.text, triggers):
                    await port.handoff("")
                    conversation.handoff = True
                else:
                    scenario = session.query(Scenario).filter_by(active=True).first()
                    if scenario and scenario.published:
                        changed = (
                            conversation.scenario_id != scenario.id
                            or conversation.version != scenario.version
                        )
                        if incoming and (changed or not conversation.node_id):
                            await port.emit(
                                "Эта кнопка устарела. Напишите «меню», чтобы начать заново."
                            )
                        else:
                            if changed:
                                conversation.node_id = ""
                                conversation.variables = {}
                            variables = dict(conversation.variables)
                            if "first_name" not in variables:
                                variables["first_name"] = variables[
                                    "user_name"
                                ] = await vk_api.get_user_name(user_id)
                            state = {
                                "node_id": conversation.node_id,
                                "version": scenario.version,
                                "variables": variables,
                                "handoff": conversation.handoff,
                                "nonce": variables.pop("_nonce", ""),
                            }
                            # handoff notification reads the answers accumulated in this turn.
                            conversation.variables = state["variables"]
                            await advance(
                                scenario.published,
                                state,
                                event.text,
                                incoming,
                                port,
                                restart=restart,
                            )
                            conversation.scenario_id, conversation.version = (
                                scenario.id,
                                scenario.version,
                            )
                            conversation.node_id, conversation.handoff = (
                                state["node_id"],
                                state.get("handoff", False),
                            )
                            conversation.variables = dict(
                                state["variables"], _nonce=state.get("nonce", "")
                            )
                    else:
                        conversation.node_id = ""
                        if incoming:
                            await port.emit(
                                "Сценарий сейчас недоступен. Напишите ваш вопрос сообщением."
                            )
                        else:
                            await port.emit(
                                values.get("chat_greeting")
                                or get_settings().chat_greeting,
                                keyboard={"one_time": False, "buttons": []},
                            )
                event.status, event.error = "done", port.warning
                session.commit()
            except Exception as error:
                session.rollback()
                logger.exception("Dialog event failed: %s", key)
                event = session.query(DialogEvent).filter_by(
                    event_key=key
                ).first() or DialogEvent(
                    event_key=key,
                    user_id=user_id,
                    text=str(message.get("text", ""))[:4000],
                )
                event.status, event.error = "failed", str(error)[:1000]
                session.add(event)
                session.commit()
                raise
