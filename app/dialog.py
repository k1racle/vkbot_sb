import asyncio
import hashlib
import json
import logging
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy import update

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
from .operators import configured_operators, reset_handoff

logger = logging.getLogger(__name__)
_locks = weakref.WeakValueDictionary()
# Bound open synchronous DB transactions while VK requests are in flight.
CALLBACK_SLOTS = asyncio.Semaphore(4)


def user_lock(user_id):
    return _locks.setdefault(user_id, asyncio.Lock())


def truth(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def incoming_payload(message):
    value = message.get("payload") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def numeric_id(value):
    try:
        return int(value) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def lock_conversation(session, user_id):
    if session.bind.dialect.name == "postgresql":
        session.execute(
            sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_id}
        )


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


class LivePort:
    def __init__(self, session, user_id, event_key, values, incoming_message_id=0):
        self.session, self.user_id, self.event_key, self.values = (
            session,
            user_id,
            event_key,
            values,
        )
        self.counter = 0
        self.warning = ""
        self.incoming_message_id = incoming_message_id

    def nonce(self, step):
        return hashlib.sha256(f"{self.event_key}:{step}".encode()).hexdigest()[:16]

    async def emit(
        self,
        message,
        keyboard=None,
        media_id="",
        attachment="",
        recipient=None,
        preserve_keyboard=False,
    ):
        if keyboard is None and not preserve_keyboard:
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
        operators = configured_operators(self.values, settings)
        conversation = self.session.get(Conversation, self.user_id)
        reset_handoff(conversation)
        conversation.handoff = True
        conversation.handoff_token = self.nonce("handoff")
        conversation.handoff_started_at = int(time.time())
        conversation.handoff_message_id = self.incoming_message_id
        if not operators:
            await self.emit(
                "Напишите ваш вопрос здесь. Он останется в сообщениях сообщества для менеджера."
            )
            return
        await self.emit(
            message or self.values.get("operator_ack") or settings.operator_ack,
            keyboard={"one_time": False, "buttons": []},
        )
        details = "\n".join(
            f"{k}: {v}"
            for k, v in conversation.variables.items()
            if k not in {"first_name", "user_name"} and not k.startswith("_")
        )
        keyboard = {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Взять в работу",
                            "payload": json.dumps(
                                {
                                    "action": "operator_claim",
                                    "client_id": self.user_id,
                                    "token": conversation.handoff_token,
                                }
                            ),
                        },
                        "color": "positive",
                    }
                ]
            ],
        }
        for operator in operators:
            await self.notify(
                operator,
                f"Нужен менеджер: https://vk.com/id{self.user_id}\n"
                "Нажмите «Взять в работу» или ответьте клиенту из сообщений сообщества. "
                "Обращение закрепится за первым менеджером.\n" + details[:3000],
                keyboard=keyboard,
            )

    async def notify(self, operator, message, keyboard=None):
        """A blocked DM or a network failure must not undo a handoff/assignment."""
        for attempt in range(3):
            try:
                await self.emit(
                    message,
                    recipient=operator,
                    keyboard=keyboard,
                    preserve_keyboard=True,
                )
                return
            except (vk_api.VkApiError, httpx.HTTPError) as error:
                if (
                    isinstance(error, vk_api.VkApiError)
                    and str(error).startswith("6:")
                    and attempt < 2
                ):
                    self.counter -= (
                        1  # Retry this notification with the same random_id.
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                warning = f"Уведомление менеджеру {operator} не доставлено: {error}"
                self.warning = (self.warning + "\n" + warning).strip()[:1000]
                logger.warning(
                    "Operator notification failed for user %s, operator %s",
                    self.user_id,
                    operator,
                )
                return


async def handle_operator_reply(payload):
    """VK message_reply identifies a human sender via optional admin_author_id."""
    obj = payload.get("object") or {}
    message = obj.get("message", obj)
    if (
        numeric_id(payload.get("group_id")) != get_settings().vk_group_id
        or numeric_id(message.get("from_id")) != -get_settings().vk_group_id
        or not message.get("out")
        or numeric_id(message.get("admin_author_id")) <= 0
    ):
        # Bot sends and clients without author information cannot claim tickets.
        return
    await assign_operator(payload, message, numeric_id(message.get("admin_author_id")))


async def assign_operator(payload, message, operator, claim=None):
    user_id = numeric_id(
        claim.get("client_id") if claim is not None else message.get("peer_id")
    )
    if not 0 < user_id < 2_000_000_000:
        return  # Never handle chat/group peers as customers.
    event_id = (
        payload.get("event_id")
        or message.get("conversation_message_id")
        or message.get("id")
    )
    if not event_id:
        return
    kind = "operator_claim" if claim is not None else "operator_reply"
    key = f"{kind}:{payload.get('group_id', 0)}:{user_id}:{operator}:{event_id}"[:160]
    async with user_lock(user_id), CALLBACK_SLOTS:
        with SessionLocal() as session:
            lock_conversation(session, user_id)
            existing = session.query(DialogEvent).filter_by(event_key=key).first()
            if existing:
                return
            values = read_settings(session)
            operators = configured_operators(values, get_settings())
            if operator not in operators:
                return  # Only currently configured managers may take a customer.
            row = session.get(Conversation, user_id)
            port = LivePort(session, user_id, key, values)
            if claim is not None:
                valid = bool(
                    row
                    and row.handoff
                    and row.handoff_token
                    and claim.get("token") == row.handoff_token
                )
            else:
                # Ignore delayed responses from an earlier, already closed request.
                valid = bool(
                    row
                    and row.handoff
                    and numeric_id(message.get("date")) >= row.handoff_started_at
                    and (
                        not row.handoff_message_id
                        or numeric_id(message.get("conversation_message_id"))
                        > row.handoff_message_id
                    )
                )
            if not valid:
                if claim is not None:
                    await port.notify(
                        operator, "Это обращение уже закрыто или кнопка устарела."
                    )
                return
            event = DialogEvent(
                event_key=key,
                user_id=user_id,
                kind=kind,
                text=(
                    f"Менеджер id{operator}: "
                    + (
                        "Взять в работу"
                        if claim is not None
                        else str(message.get("text", ""))
                    )
                )[:4000],
                status="done",
            )
            session.add(event)
            # Conditional UPDATE also protects ownership if competing callbacks run
            # in different processes. No later manager can overwrite the winner.
            won = (
                session.execute(
                    update(Conversation)
                    .where(
                        Conversation.user_id == user_id,
                        Conversation.handoff.is_(True),
                        Conversation.assigned_operator_id.is_(None),
                        Conversation.handoff_token == row.handoff_token,
                    )
                    .values(
                        assigned_operator_id=operator,
                        assigned_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount
                == 1
            )
            session.refresh(row)
            owner = row.assigned_operator_id
            session.commit()  # Delivery failures must never release ownership.
            if won:
                for recipient in operators:
                    notice = (
                        f"Обращение https://vk.com/id{user_id} закреплено за вами. "
                        "Отвечайте из сообщений сообщества VK."
                        if recipient == operator
                        else f"Обращение https://vk.com/id{user_id} уже взял менеджер "
                        f"https://vk.com/id{operator}. Повторно отвечать не нужно."
                    )
                    await port.notify(recipient, notice)
            elif claim is not None or owner != operator:
                await port.notify(
                    operator,
                    f"Обращение уже закреплено за менеджером https://vk.com/id{owner}.",
                )
            event.error = port.warning
            session.commit()


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
    incoming = incoming_payload(message)
    if incoming.get("action") == "operator_claim":
        if numeric_id(payload.get("group_id")) == get_settings().vk_group_id:
            await assign_operator(payload, message, user_id, claim=incoming)
        return
    key = f"{payload.get('group_id', 0)}:{user_id}:{event_id}"[:160]
    lock = user_lock(user_id)
    async with lock, CALLBACK_SLOTS:
        with SessionLocal() as session:
            # Serialize messages of one client across app processes on PostgreSQL too.
            lock_conversation(session, user_id)
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
            port = LivePort(
                session,
                user_id,
                key,
                values,
                incoming_message_id=numeric_id(message.get("conversation_message_id")),
            )
            try:
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
                    reset_handoff(conversation)
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
