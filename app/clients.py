"""Minimal CRM for known customers; no scraping or access to hidden profiles."""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from . import db

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def insert_once(session, model, values, keys):
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    session.execute(
        insert(model).values(**values).on_conflict_do_nothing(index_elements=keys)
    )


def ensure_client(session, user_id):
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not 0 < user_id < 2_000_000_000
    ):
        return None
    insert_once(session, db.Client, {"user_id": user_id}, ["user_id"])
    return session.get(db.Client, user_id)


def record_outgoing(user_id, random_id):
    """Called only AFTER VK accepted a send. Audit failure must not resend it."""
    try:
        with db.SessionLocal() as session:
            client = ensure_client(session, user_id)
            if client is None:
                return
            key = f"send:{user_id}:{random_id}"
            insert_once(
                session,
                db.BotMessage,
                {"event_key": key, "user_id": user_id},
                ["event_key"],
            )
            client.bot_contacted_at = now()
            client.contact_source = "bot"
            client.messages_allowed = True
            session.commit()
    except SQLAlchemyError:
        logger.warning("Could not persist outgoing-message audit for user %s", user_id)


def record_incoming(user_id):
    with db.SessionLocal() as session:
        client = ensure_client(session, user_id)
        if client:
            client.last_incoming_at = now()
            session.commit()


def safe_photo(value):
    value = str(value or "")[:2000]
    try:
        url = urlparse(value)
    except ValueError:
        return ""
    return (
        value
        if url.scheme == "https"
        and url.hostname
        and not url.username
        and not url.password
        else ""
    )


def clean_phone(value):
    value = str(value or "").strip()[:80]
    digits = re.sub(r"\D", "", value)
    return (
        value
        if 7 <= len(digits) <= 20 and re.fullmatch(r"[+\d\s().\-]+", value)
        else ""
    )


def save_profile(session, profile):
    user_id = profile.get("id")
    client = session.get(db.Client, user_id) if isinstance(user_id, int) else None
    if client is None:
        return  # Never discover unrelated people through an API response.
    client.first_name = str(profile.get("first_name") or "")[:120]
    client.last_name = str(profile.get("last_name") or "")[:120]
    client.photo_url = safe_photo(profile.get("photo_100") or profile.get("photo_200"))
    client.deactivated = bool(profile.get("deactivated"))
    phone = clean_phone(profile.get("mobile_phone")) or clean_phone(
        profile.get("home_phone")
    )
    if phone:
        client.phone, client.phone_source = phone, "vk"
    elif client.phone_source == "vk":
        client.phone, client.phone_source = "", ""  # Do not retain a now-hidden number.
    if not phone:
        conversation = session.get(db.Conversation, user_id)
        if conversation:
            for key in ("phone", "telephone", "телефон"):
                phone = clean_phone(conversation.variables.get(key))
                if phone:
                    client.phone, client.phone_source = phone, "dialog"
                    break
    client.profile_updated_at, client.profile_requested, client.profile_error = (
        now(),
        False,
        "",
    )


def backfill_clients():
    """Known dialog/comment clients; only proven deliveries qualify for mailings.

    A processed incoming dialog event is NOT evidence that a reply was sent.
    Old greetings cannot be reconstructed from that event alone.
    """
    with db.SessionLocal() as session:
        ids = select(db.Conversation.user_id).union(
            select(db.ProcessedComment.user_id),
            select(db.PromoDelivery.user_id),
            select(db.BotMessage.user_id),
        )
        for (user_id,) in session.execute(ids):
            client = ensure_client(session, user_id)
            if not client:
                continue
            if not client.bot_contacted_at:
                sent = (
                    session.query(func.max(db.ProcessedComment.created_at))
                    .filter_by(user_id=user_id, status="sent")
                    .scalar()
                )
                promo = (
                    session.query(func.max(db.PromoDelivery.created_at))
                    .filter_by(user_id=user_id)
                    .scalar()
                )
                audit = (
                    session.query(func.max(db.BotMessage.created_at))
                    .filter_by(user_id=user_id)
                    .scalar()
                )
                dates = [d for d in (sent, promo, audit) if d]
                if dates:
                    client.bot_contacted_at, client.contact_source = (
                        max(dates),
                        "saved_delivery",
                    )
            conversation = session.get(db.Conversation, user_id)
            if conversation and not client.first_name:
                client.first_name = str(conversation.variables.get("first_name") or "")[
                    :120
                ]
        session.commit()


def eligible_query(session, operators=()):
    return session.query(db.Client).filter(
        db.Client.bot_contacted_at.is_not(None),
        db.Client.unsubscribed.is_(False),
        db.Client.deactivated.is_(False),
        db.Client.user_id.not_in(list(operators)),
    )


def list_query(session, search="", contacted=False):
    query = session.query(db.Client)
    if contacted:
        query = query.filter(db.Client.bot_contacted_at.is_not(None))
    if search.strip():
        value = search.strip()[:120]
        pattern = (
            "%"
            + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        terms = [
            db.Client.first_name.ilike(pattern, escape="\\"),
            db.Client.last_name.ilike(pattern, escape="\\"),
            (db.Client.first_name + " " + db.Client.last_name).ilike(
                pattern, escape="\\"
            ),
            db.Client.phone.ilike(pattern, escape="\\"),
        ]
        if value.isascii() and value.isdigit() and len(value) < 11:
            terms.append(db.Client.user_id == int(value))
        query = query.filter(or_(*terms))
    return query


def serialize(client):
    return {
        "user_id": client.user_id,
        "first_name": client.first_name,
        "last_name": client.last_name,
        "photo_url": safe_photo(client.photo_url),
        "vk_url": f"https://vk.ru/id{client.user_id}",
        "phone": client.phone,
        "phone_source": client.phone_source,
        "deactivated": client.deactivated,
        "bot_contacted_at": str(client.bot_contacted_at)
        if client.bot_contacted_at
        else None,
        "contact_source": client.contact_source,
        "unsubscribed": client.unsubscribed,
        "messages_allowed": client.messages_allowed,
        "profile_updated_at": str(client.profile_updated_at)
        if client.profile_updated_at
        else None,
        "profile_requested": client.profile_requested,
        "profile_error": client.profile_error,
    }
