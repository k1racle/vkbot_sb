from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool, StaticPool

from .config import get_base_settings, get_settings


class Base(DeclarativeBase):
    pass


class ProcessedComment(Base):
    # A new table avoids destructively rebuilding the old globally-unique
    # comment_id column: wall and video comments can have the same numeric ID.
    __tablename__ = "comment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    source_type: Mapped[str] = mapped_column(String(16), default="wall")
    owner_id: Mapped[int] = mapped_column(BigInteger)
    comment_id: Mapped[int] = mapped_column(Integer, index=True)
    # VK post_id for wall events, video_id for video events.
    post_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 0 means the single fallback campaign. Existing positive IDs stay unchanged.
    post_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, default=0)
    title: Mapped[str] = mapped_column(String(120), default="")
    promo_code: Mapped[str] = mapped_column(String(120), default="")
    shop_url: Mapped[str] = mapped_column(String(500), default="")
    promo_message: Mapped[str] = mapped_column(Text, default="")
    attachment_path: Mapped[str] = mapped_column(String(500), default="")
    attachment_name: Mapped[str] = mapped_column(String(255), default="")
    attachment_type: Mapped[str] = mapped_column(String(120), default="")
    stop_words: Mapped[str] = mapped_column(Text, default="")
    plus_words: Mapped[str] = mapped_column(Text, default="")
    min_comment_length: Mapped[int] = mapped_column(Integer, default=1)
    one_promo_per_user: Mapped[bool] = mapped_column(default=True)
    delivery_mode: Mapped[str] = mapped_column(String(24), default="direct")
    public_reply_variants: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class PendingGift(Base):
    __tablename__ = "pending_gifts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_id: Mapped[int] = mapped_column(Integer, index=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    # At most one outstanding invitation per customer/campaign. Completed rows
    # retain history, but release this key so repeatable campaigns can issue again.
    active_key: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending")
    awaiting_subscription: Mapped[bool] = mapped_column(default=False)
    invitation_text: Mapped[str] = mapped_column(Text, default="")
    delivery_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="Новый сценарий")
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    published: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(default=0)
    revision: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[object] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Conversation(Base):
    __tablename__ = "conversations"

    user_id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(default=0)
    node_id: Mapped[str] = mapped_column(String(64), default="")
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    handoff: Mapped[bool] = mapped_column(default=False)
    assigned_operator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    handoff_token: Mapped[str] = mapped_column(String(64), default="")
    handoff_started_at: Mapped[int] = mapped_column(BigInteger, default=0)
    handoff_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[object] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DialogEvent(Base):
    __tablename__ = "dialog_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    user_id: Mapped[int] = mapped_column(index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="processing")
    kind: Mapped[str] = mapped_column(String(32), default="incoming")
    gift_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    path: Mapped[str] = mapped_column(String(500))


class PromoDelivery(Base):
    __tablename__ = "promo_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(index=True)
    campaign_id: Mapped[int] = mapped_column(index=True)
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class Client(Base):
    __tablename__ = "clients"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    photo_url: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(80), default="")
    phone_source: Mapped[str] = mapped_column(String(32), default="")
    deactivated: Mapped[bool] = mapped_column(default=False)
    bot_contacted_at: Mapped[object | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    contact_source: Mapped[str] = mapped_column(String(32), default="")
    last_incoming_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    messages_allowed: Mapped[bool | None] = mapped_column(nullable=True)
    unsubscribed: Mapped[bool] = mapped_column(default=False, index=True)
    profile_requested: Mapped[bool] = mapped_column(default=True, index=True)
    profile_updated_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    profile_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class BotMessage(Base):
    __tablename__ = "bot_messages"

    event_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Only successful outgoing private sends. Never contains credentials.
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    media_id: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    consent_confirmed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"
    __table_args__ = (UniqueConstraint("broadcast_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    lease_until: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class WorkLease(Base):
    __tablename__ = "work_leases"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32), default="")
    until: Mapped[object | None] = mapped_column(DateTime, nullable=True)


def make_engine():
    settings = get_base_settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (
        None,
        "",
        ":memory:",
    ):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(settings.database_url, connect_args=connect_args)


engine = make_engine()
_project_engines: WeakKeyDictionary = WeakKeyDictionary()
_project_engine_lock = RLock()


def get_project_engine(project) -> Engine:
    """Return an isolated engine without rebinding any global engine or session.

    PostgreSQL connections have exactly one application schema in search_path;
    even raw migration SQL cannot fall through to legacy/public tables. New
    engines use NullPool so idle projects do not reserve database connections.
    """
    from .projects import validate_project_id

    validate_project_id(project.id)
    if project.is_legacy:
        return engine
    with _project_engine_lock:
        cached = _project_engines.setdefault(engine, {})
        if project.id in cached:
            return cached[project.id]
        backend = engine.url.get_backend_name()
        if backend == "postgresql":
            schema = f"project_{project.id}"
            quoted = engine.dialect.identifier_preparer.quote_identifier(schema)
            with engine.begin() as connection:
                connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quoted}"))
            isolated = create_engine(engine.url, poolclass=NullPool)

            @event.listens_for(isolated, "connect", insert=True)
            def set_project_schema(dbapi_connection, _connection_record):
                # SET must survive transaction rollback, including the dialect's
                # initial introspection. Register before dialect initialization.
                previous = dbapi_connection.autocommit
                dbapi_connection.autocommit = True
                try:
                    with dbapi_connection.cursor() as cursor:
                        cursor.execute(f"SET SESSION search_path TO {quoted}")
                finally:
                    dbapi_connection.autocommit = previous

        elif backend == "sqlite":
            database = engine.url.database
            if database in (None, "", ":memory:"):
                # Independent memory databases are useful for isolated tests;
                # ordinary development uses separate files beside the legacy DB.
                isolated = create_engine(
                    "sqlite://",
                    poolclass=StaticPool,
                    connect_args={"check_same_thread": False},
                )
            else:
                if database.startswith("file:"):
                    raise ValueError(
                        "Project storage requires a regular SQLite database path"
                    )
                path = Path(database).resolve().with_name(f"project_{project.id}.db")
                isolated = create_engine(
                    engine.url.set(database=str(path)),
                    poolclass=NullPool,
                    connect_args={"check_same_thread": False},
                )
        else:
            raise ValueError("Project storage supports PostgreSQL and SQLite only")
        cached[project.id] = isolated
        return isolated


def dispose_project_engines() -> None:
    """Release cached project engines (shutdown/tests); never dispose legacy."""
    with _project_engine_lock:
        for cached in _project_engines.values():
            for isolated in cached.values():
                isolated.dispose()
        _project_engines.clear()


def current_engine() -> Engine:
    from .projects import current_project

    project = current_project.get()
    return engine if project is None else get_project_engine(project)


def SessionLocal() -> Session:
    """Capture the project's engine when constructing each independent session.

    No context retains the old legacy behavior for scripts/tests. HTTP handlers
    and workers must establish project_scope before accessing project data.
    """
    return Session(bind=current_engine(), expire_on_commit=False)


def init_db(
    target_engine: Engine | None = None,
    migration_owner_group_id: int | None = None,
) -> None:
    # This is a local variable, deliberately never a mutation of db.engine.
    engine = target_engine if target_engine is not None else current_engine()
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    backfill_subscription = "awaiting_subscription" not in {
        column["name"] for column in inspector.get_columns("pending_gifts")
    }
    if "campaigns" in inspector.get_table_names():
        existing = {column["name"] for column in inspector.get_columns("campaigns")}
        additions = {
            "attachment_path": "VARCHAR(500) DEFAULT ''",
            "attachment_name": "VARCHAR(255) DEFAULT ''",
            "attachment_type": "VARCHAR(120) DEFAULT ''",
            "stop_words": "TEXT DEFAULT ''",
            "plus_words": "TEXT NOT NULL DEFAULT ''",
            "min_comment_length": "INTEGER DEFAULT 1",
            "one_promo_per_user": "BOOLEAN DEFAULT TRUE",
            "delivery_mode": "VARCHAR(24) NOT NULL DEFAULT 'direct'",
            "public_reply_variants": "JSON NOT NULL DEFAULT '[]'",
        }
        with engine.begin() as connection:
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(
                        text(f"ALTER TABLE campaigns ADD COLUMN {column} {definition}")
                    )
    # Additive upgrade: keep existing dialogs and campaigns intact.
    for table, additions in {
        "pending_gifts": {
            "awaiting_subscription": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "conversations": {
            "assigned_operator_id": "BIGINT",
            "assigned_at": "TIMESTAMP",
            "handoff_token": "VARCHAR(64) NOT NULL DEFAULT ''",
            "handoff_started_at": "BIGINT NOT NULL DEFAULT 0",
            "handoff_message_id": "BIGINT NOT NULL DEFAULT 0",
        },
        "dialog_events": {
            "kind": "VARCHAR(32) NOT NULL DEFAULT 'incoming'",
            "gift_id": "VARCHAR(32)",
        },
    }.items():
        existing = {column["name"] for column in inspect(engine).get_columns(table)}
        with engine.begin() as connection:
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    )
    if backfill_subscription:
        # In the previous invitation release, a completed gift request with an
        # outstanding gift meant membership was missing. Preserve those opt-ins;
        # never arm gifts whose owners have only commented, not entered the chat.
        with engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE pending_gifts SET awaiting_subscription = TRUE
                WHERE status = 'pending' AND EXISTS (
                    SELECT 1 FROM dialog_events e
                    WHERE e.gift_id = pending_gifts.id
                      AND e.user_id = pending_gifts.user_id
                      AND e.kind = 'gift' AND e.status = 'done'
                )
            """)
            )
    if "processed_comments" in inspect(engine).get_table_names():
        # Preserve the old table as an archive. Copy records and delivery history
        # once per event, with the same identity the new callback handler uses.
        if migration_owner_group_id is None:
            from .projects import current_project

            project = current_project.get()
            migration_owner_group_id = (
                project.group_id if project is not None else get_settings().vk_group_id
            )
        if migration_owner_group_id <= 0:
            raise ValueError(
                "A positive migration owner group is required for comment history"
            )
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO comment_events
                    (event_key, source_type, owner_id, comment_id, post_id, user_id,
                     campaign_id, status, error, created_at)
                SELECT 'wall:' || CAST(:owner AS TEXT) || ':' || CAST(p.post_id AS TEXT)
                              || ':' || CAST(p.comment_id AS TEXT),
                       'wall', :owner, p.comment_id, p.post_id, p.user_id,
                       c.id, p.status, p.error, p.created_at
                FROM processed_comments p
                LEFT JOIN campaigns c ON c.post_id = p.post_id
                WHERE 1=1
                ON CONFLICT (event_key) DO NOTHING
            """),
                {"owner": -migration_owner_group_id},
            )


def already_processed(session: Session, event_key: str) -> bool:
    return (
        session.query(ProcessedComment).filter_by(event_key=event_key).first()
        is not None
    )


def already_sent_to_user(session: Session, user_id: int) -> bool:
    return (
        session.query(ProcessedComment)
        .filter_by(user_id=user_id, status="sent")
        .first()
        is not None
    )


def read_settings(session: Session) -> dict[str, str]:
    return {item.key: item.value for item in session.query(BotSetting).all()}


def save_settings(session: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        item = session.get(BotSetting, key)
        if item is None:
            session.add(BotSetting(key=key, value=value))
        else:
            item.value = value
    session.commit()
