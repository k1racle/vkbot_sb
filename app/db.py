from pathlib import Path

from sqlalchemy import DateTime, Integer, String, Text, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


class ProcessedComment(Base):
    __tablename__ = "processed_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, server_default=func.now())


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def make_engine():
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///./"):
        Path("data").mkdir(exist_ok=True)
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def already_processed(session: Session, comment_id: int) -> bool:
    return session.query(ProcessedComment).filter_by(comment_id=comment_id).first() is not None


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
