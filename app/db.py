from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


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
    min_comment_length: Mapped[int] = mapped_column(Integer, default=1)
    one_promo_per_user: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
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


def make_engine():
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///./"):
        Path("data").mkdir(exist_ok=True)
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(settings.database_url, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if "campaigns" in inspector.get_table_names():
        existing = {column["name"] for column in inspector.get_columns("campaigns")}
        additions = {
            "attachment_path": "VARCHAR(500) DEFAULT ''",
            "attachment_name": "VARCHAR(255) DEFAULT ''",
            "attachment_type": "VARCHAR(120) DEFAULT ''",
            "stop_words": "TEXT DEFAULT ''",
            "min_comment_length": "INTEGER DEFAULT 1",
            "one_promo_per_user": "BOOLEAN DEFAULT TRUE",
        }
        with engine.begin() as connection:
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(
                        text(f"ALTER TABLE campaigns ADD COLUMN {column} {definition}")
                    )
    # Additive upgrade: keep existing dialogs and campaigns intact.
    for table, additions in {
        "conversations": {
            "assigned_operator_id": "BIGINT",
            "assigned_at": "TIMESTAMP",
            "handoff_token": "VARCHAR(64) NOT NULL DEFAULT ''",
            "handoff_started_at": "BIGINT NOT NULL DEFAULT 0",
            "handoff_message_id": "BIGINT NOT NULL DEFAULT 0",
        },
        "dialog_events": {"kind": "VARCHAR(32) NOT NULL DEFAULT 'incoming'"},
    }.items():
        existing = {column["name"] for column in inspect(engine).get_columns(table)}
        with engine.begin() as connection:
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    )
    if "processed_comments" in inspect(engine).get_table_names():
        # Preserve the old table as an archive. Copy records and delivery history
        # once per event, with the same identity the new callback handler uses.
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
                {"owner": -get_settings().vk_group_id},
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
