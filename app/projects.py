"""Project registry, credentials and task-local project selection.

The registry lives in db.engine's default schema, on independent metadata.
Only the initial ENV bootstrap may adopt legacy storage. Later projects always
get new storage, including the first project created on an empty installation.

Back up PROJECTS_ENCRYPTION_KEY separately, or back up the generated
PROJECTS_KEY_FILE (default data/projects.key) along with the database.
Losing this key makes existing credentials unrecoverable. Never substitute the
admin session secret, regenerate a missing key over ciphertext, or log secrets.

ContextVars propagate into asyncio tasks, asyncio.to_thread and AnyIO worker
threads. For raw executors/threads use bind_project_context at submission time.
"""

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from threading import RLock

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Integer,
    String,
    Text,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class ProjectValidationError(ValueError):
    pass


class ProjectNotFoundError(ValueError):
    pass


class ProjectEncryptionError(ValueError):
    pass


class RegistryBase(DeclarativeBase):
    """Never add registry tables to the project model metadata."""


class _ProjectRow(RegistryBase):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("group_id > 0", name="project_group_positive"),
        CheckConstraint(
            "legacy_slot IS NULL OR legacy_slot = 1", name="project_legacy_slot"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    group_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    encrypted_token: Mapped[str] = mapped_column(Text)
    encrypted_secret: Mapped[str] = mapped_column(Text)
    encrypted_confirmation: Mapped[str] = mapped_column(Text)
    encrypted_defaults: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    is_deleted: Mapped[bool] = mapped_column(
        default=False, server_default=text("FALSE")
    )
    # UNIQUE allows many NULLs but only one legacy owner on both backends.
    legacy_slot: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)


class _RegistryState(RegistryBase):
    __tablename__ = "project_registry_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_check: Mapped[str] = mapped_column(Text)


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    group_id: int
    encrypted_token: str = field(repr=False)
    encrypted_secret: str = field(repr=False)
    encrypted_confirmation: str = field(repr=False)
    encrypted_defaults: str = field(repr=False)
    enabled: bool
    is_legacy: bool
    _cipher: Fernet = field(repr=False, compare=False)
    is_deleted: bool = False

    @property
    def token(self) -> str:
        return _decrypt(self._cipher, self.encrypted_token)

    @property
    def video_token(self) -> str:
        # Optional credential kept inside the already-encrypted private payload.
        # It is deliberately NOT overlaid onto general bot settings or exports.
        values = json.loads(_decrypt(self._cipher, self.encrypted_defaults))
        return values.get("_video_user_token", "")

    @property
    def secret(self) -> str:
        return _decrypt(self._cipher, self.encrypted_secret)

    @property
    def confirmation(self) -> str:
        return _decrypt(self._cipher, self.encrypted_confirmation)

    @property
    def callback_secret(self) -> str:
        return self.secret

    @property
    def confirmation_code(self) -> str:
        return self.confirmation

    @property
    def vk_group_id(self) -> int:
        return self.group_id

    @property
    def vk_group_token(self) -> str:
        return self.token

    @property
    def vk_callback_secret(self) -> str:
        return self.secret

    @property
    def vk_confirmation_code(self) -> str:
        return self.confirmation


current_project: ContextVar[Project | None] = ContextVar(
    "current_project", default=None
)
_registry_lock = RLock()
_KEY_CHECK = b"vk-bot-project-registry-v1"
_PROJECT_SETTING_FIELDS = frozenset(
    {
        "promo_code",
        "promo_message",
        "shop_url",
        "promo_attachments",
        "allowed_post_ids",
        "stop_words",
        "min_comment_length",
        "one_promo_per_user",
        "test_mode",
        "test_trigger_phrase",
        "admin_test_user_id",
        "chat_enabled",
        "chat_greeting",
        "operator_user_id",
        "operator_trigger_words",
        "operator_ack",
    }
)


@contextmanager
def project_scope(project: Project | None):
    if project is not None and not isinstance(project, Project):
        raise TypeError("project_scope requires a Project or None")
    token = current_project.set(project)
    try:
        yield project
    finally:
        current_project.reset(token)


def bind_project_context(function, /, *args, **kwargs):
    """Capture context now for Thread/Executor submission; safe to reuse."""
    context = copy_context()

    @wraps(function)
    def call():
        return context.copy().run(function, *args, **kwargs)

    return call


def _base_settings():
    # Lazy imports keep config's project-aware get_settings free of cycles.
    from .config import get_base_settings

    return get_base_settings()


def _setting(settings, name: str, default: str) -> str:
    return getattr(settings, name, os.environ.get(name.upper(), default))


def _decrypt(cipher: Fernet, ciphertext: str) -> str:
    try:
        return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, AttributeError):
        raise ProjectEncryptionError(
            "Не удалось расшифровать данные проектов. Восстановите исходный ключ PROJECTS_ENCRYPTION_KEY или его резервную копию."
        ) from None


def _load_cipher(settings, *, existing_ciphertext: bool) -> Fernet:
    configured = _setting(settings, "projects_encryption_key", "")
    if configured:
        key = configured
    else:
        path = Path(_setting(settings, "projects_key_file", "data/projects.key"))
        try:
            key = path.read_bytes().strip()
        except FileNotFoundError:
            if existing_ciphertext:
                raise ProjectEncryptionError(
                    "Ключ шифрования проектов отсутствует. Восстановите файл ключа из резервной копии или задайте исходный PROJECTS_ENCRYPTION_KEY."
                ) from None
            path.parent.mkdir(parents=True, exist_ok=True)
            generated = Fernet.generate_key()
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = path.read_bytes().strip()
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(generated + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                key = generated
        except OSError:
            raise ProjectEncryptionError(
                "Не удалось прочитать файл ключа шифрования проектов."
            ) from None
    try:
        return Fernet(key)
    except (ValueError, TypeError, UnicodeError):
        raise ProjectEncryptionError(
            "PROJECTS_ENCRYPTION_KEY должен содержать корректный ключ Fernet."
        ) from None


def _checked_cipher(session: Session) -> Fernet:
    state = session.get(_RegistryState, 1)
    if state is None:
        raise ProjectEncryptionError(
            "Реестр проектов не инициализирован. Сначала выполните init_registry()."
        )
    cipher = _load_cipher(_base_settings(), existing_ciphertext=True)
    if _decrypt(cipher, state.key_check) != _KEY_CHECK.decode():
        raise ProjectEncryptionError("Проверка ключа шифрования проектов не пройдена.")
    return cipher


@contextmanager
def _registry_write():
    """Serialize registry writes across threads and processes, not project I/O."""
    from . import db

    with _registry_lock, db.engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(861143419104)"))
        with Session(bind=connection, expire_on_commit=False) as session:
            yield session
            session.flush()
        connection.commit()


def validate_project_id(project_id: int) -> int:
    if type(project_id) is not int or not 0 < project_id <= 2147483647:
        raise ProjectValidationError("Некорректный ID проекта.")
    return project_id


def _validate_group_id(group_id: int | str) -> int:
    if isinstance(group_id, str) and re.fullmatch(r"[0-9]{1,19}", group_id.strip()):
        group_id = int(group_id.strip())
    if type(group_id) is not int or not 0 < group_id <= 9223372036854775807:
        raise ProjectValidationError(
            "ID сообщества должен быть положительным целым числом."
        )
    return group_id


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ProjectValidationError(
            "Название проекта должно содержать от 1 до 120 символов."
        )
    return name.strip()


def _validate_credential(
    value: str,
    name: str,
    *,
    required: bool = False,
    normalize: bool = True,
) -> str:
    label = {
        "token": "Токен сообщества",
        "video_token": "Пользовательский токен для видео",
        "secret": "Секрет Callback API",
        "callback_secret": "Секрет Callback API",
        "confirmation": "Код подтверждения",
        "confirmation_code": "Код подтверждения",
    }[name]
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label}: требуется строка.")
    limit = 4096 if name in {"token", "video_token"} else 1024
    if len(value) > limit:
        raise ProjectValidationError(f"{label}: допускается не более {limit} символов.")
    if required and not value.strip():
        raise ProjectValidationError(
            f"{label}: заполните поле перед включением проекта."
        )
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ProjectValidationError(f"{label}: некорректный текст UTF-8.") from None
    return value.strip() if normalize else value


def _to_project(row: _ProjectRow, cipher: Fernet) -> Project:
    # Verify all fields immediately; never return a partly decryptable project.
    for ciphertext in (
        row.encrypted_token,
        row.encrypted_secret,
        row.encrypted_confirmation,
        row.encrypted_defaults,
    ):
        _decrypt(cipher, ciphertext)
    validate_project_id(row.id)
    return Project(
        id=row.id,
        name=row.name,
        group_id=row.group_id,
        encrypted_token=row.encrypted_token,
        encrypted_secret=row.encrypted_secret,
        encrypted_confirmation=row.encrypted_confirmation,
        encrypted_defaults=row.encrypted_defaults,
        enabled=row.enabled,
        is_legacy=row.legacy_slot == 1,
        is_deleted=row.is_deleted,
        _cipher=cipher,
    )


def _setting_defaults(base) -> dict:
    return {
        name: type(base).model_fields[name].get_default(call_default_factory=True)
        for name in _PROJECT_SETTING_FIELDS
        if name in type(base).model_fields
    }


def project_settings(project: Project, base):
    """Overlay credentials and saved group defaults; keep infrastructure shared.

    model_copy deliberately bypasses BaseSettings' ENV sources. A new group must
    never inherit the legacy group's operator IDs, testing flags or chat text.
    Existing bot_settings rows still override these fallback defaults normally.
    """
    defaults = _setting_defaults(base)
    try:
        saved = json.loads(_decrypt(project._cipher, project.encrypted_defaults))
        if not isinstance(saved, dict):
            raise TypeError
    except (ValueError, TypeError):
        raise ProjectEncryptionError(
            "Не удалось прочитать сохранённые настройки проекта."
        ) from None
    defaults.update(
        {key: value for key, value in saved.items() if key in _PROJECT_SETTING_FIELDS}
    )
    defaults.update(
        vk_group_id=project.group_id,
        vk_group_token=project.token,
        vk_callback_secret=project.secret,
        vk_confirmation_code=project.confirmation,
    )
    return base.model_copy(update=defaults)


def _new_row(
    cipher,
    *,
    name,
    group_id,
    token,
    secret,
    confirmation,
    enabled,
    defaults,
    legacy=False,
):
    if type(enabled) is not bool:
        raise ProjectValidationError(
            "Состояние проекта должно быть включено или выключено."
        )
    return _ProjectRow(
        name=_validate_name(name),
        group_id=_validate_group_id(group_id),
        encrypted_token=cipher.encrypt(
            _validate_credential(
                token, "token", required=enabled, normalize=not legacy
            ).encode()
        ).decode(),
        encrypted_secret=cipher.encrypt(
            _validate_credential(
                secret, "callback_secret", required=enabled, normalize=not legacy
            ).encode()
        ).decode(),
        encrypted_confirmation=cipher.encrypt(
            _validate_credential(
                confirmation,
                "confirmation_code",
                required=enabled,
                normalize=not legacy,
            ).encode()
        ).decode(),
        encrypted_defaults=cipher.encrypt(
            json.dumps(defaults, ensure_ascii=False).encode()
        ).decode(),
        enabled=enabled,
        legacy_slot=1 if legacy else None,
    )


def _has_legacy_data(connection) -> bool:
    """Inspect only known model tables and the historical comment archive.

    Raw existence queries also work before additive migrations, when ORM column
    lists may not yet match an old table. No arbitrary application tables are read.
    """
    from . import db

    existing = set(inspect(connection).get_table_names())
    known = set(db.Base.metadata.tables) | {"processed_comments"}
    for name in sorted(existing & known):
        quoted = connection.dialect.identifier_preparer.quote_identifier(name)
        if (
            connection.execute(text(f"SELECT 1 FROM {quoted} LIMIT 1")).first()
            is not None
        ):
            return True
    return False


def init_registry(settings=None) -> list[Project]:
    """Initialize storage and bootstrap ENV once; return projects, legacy first.

    A fresh install needs no VK ENV credentials. A durable state row records the
    first initialization even when no ENV project qualifies for legacy adoption.
    Optional settings must be base (not project-overlay) settings.
    """
    from . import db

    settings = _base_settings() if settings is None else settings
    with _registry_write() as session:
        RegistryBase.metadata.create_all(session.connection())
        if "is_deleted" not in {
            c["name"] for c in inspect(session.connection()).get_columns("projects")
        }:
            session.execute(
                text(
                    "ALTER TABLE projects ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        state = session.get(_RegistryState, 1)
        rows = list(session.scalars(select(_ProjectRow)))
        group_id = getattr(settings, "vk_group_id", 0)
        token = getattr(settings, "vk_group_token", "")
        can_bootstrap = group_id > 0 and bool(token and token.strip())
        if (
            state is None
            and not rows
            and not can_bootstrap
            and _has_legacy_data(session.connection())
        ):
            raise ProjectValidationError(
                "В исходной базе уже есть данные бота. Для безопасного переноса первого проекта "
                "задайте прежние VK_GROUP_ID и VK_GROUP_TOKEN, затем повторите запуск."
            )
        cipher = _load_cipher(settings, existing_ciphertext=bool(state or rows))
        if state is not None:
            if _decrypt(cipher, state.key_check) != _KEY_CHECK.decode():
                raise ProjectEncryptionError(
                    "Проверка ключа шифрования проектов не пройдена."
                )
        else:
            if not rows and can_bootstrap:
                row = _new_row(
                    cipher,
                    name=f"VK {group_id}",
                    group_id=group_id,
                    token=token,
                    secret=getattr(settings, "vk_callback_secret", ""),
                    confirmation=getattr(settings, "vk_confirmation_code", ""),
                    enabled=bool(
                        getattr(settings, "vk_callback_secret", "").strip()
                        and getattr(settings, "vk_confirmation_code", "").strip()
                    ),
                    legacy=True,
                    defaults={
                        key: getattr(settings, key) for key in _PROJECT_SETTING_FIELDS
                    },
                )
                session.add(row)
                rows.append(row)
            session.add(
                _RegistryState(id=1, key_check=cipher.encrypt(_KEY_CHECK).decode())
            )
            session.flush()
        projects = sorted(
            (_to_project(row, cipher) for row in rows),
            key=lambda p: (not p.is_legacy, p.name, p.id),
        )
    # Legacy DDL uses the same database as the registry. Commit the bootstrap
    # first so SQLite's registry write lock is not held during its migrations.
    for project in projects:
        db.init_db(
            db.get_project_engine(project), migration_owner_group_id=project.group_id
        )
    return [project for project in projects if not project.is_deleted]


def list_projects(
    enabled_only: bool = False, *, include_deleted: bool = False
) -> list[Project]:
    from . import db

    with Session(bind=db.engine) as session:
        cipher = _checked_cipher(session)
        query = select(_ProjectRow)
        if not include_deleted:
            query = query.where(_ProjectRow.is_deleted.is_(False))
        if enabled_only:
            query = query.where(_ProjectRow.enabled.is_(True))
        projects = [_to_project(row, cipher) for row in session.scalars(query)]
        return sorted(projects, key=lambda p: (not p.is_legacy, p.name, p.id))


def get_project(project_id: int, *, include_deleted: bool = False) -> Project | None:
    from . import db

    validate_project_id(project_id)
    with Session(bind=db.engine) as session:
        cipher = _checked_cipher(session)
        row = session.get(_ProjectRow, project_id)
        return (
            None
            if row is None or (row.is_deleted and not include_deleted)
            else _to_project(row, cipher)
        )


def get_project_by_group_id(
    group_id: int | str, *, include_deleted: bool = False
) -> Project | None:
    from . import db

    group_id = _validate_group_id(group_id)
    with Session(bind=db.engine) as session:
        cipher = _checked_cipher(session)
        row = session.scalar(
            select(_ProjectRow).where(_ProjectRow.group_id == group_id)
        )
        return (
            None
            if row is None or (row.is_deleted and not include_deleted)
            else _to_project(row, cipher)
        )


def create_project(
    name: str,
    group_id: int | str,
    token: str = "",
    callback_secret: str = "",
    confirmation_code: str = "",
    enabled: bool = False,
) -> Project:
    from . import db

    try:
        with _registry_write() as session:
            cipher = _checked_cipher(session)
            row = _new_row(
                cipher,
                name=name,
                group_id=group_id,
                token=token,
                secret=callback_secret,
                confirmation=confirmation_code,
                enabled=enabled,
                defaults=_setting_defaults(_base_settings()),
            )
            if session.scalar(
                select(_ProjectRow.id).where(_ProjectRow.group_id == row.group_id)
            ):
                raise ProjectValidationError(
                    "Проект с таким ID сообщества уже существует."
                )
            session.add(row)
            session.flush()
            project = _to_project(row, cipher)
            db.init_db(
                db.get_project_engine(project),
                migration_owner_group_id=project.group_id,
            )
        return project
    except IntegrityError:
        raise ProjectValidationError(
            "Проект с таким ID сообщества уже существует."
        ) from None


def update_fetched_name(project: Project, name: str, *, force=False) -> Project:
    """Compare-and-set after VK I/O; never overwrite an intervening manual edit."""
    with _registry_write() as session:
        cipher = _checked_cipher(session)
        row = session.get(_ProjectRow, project.id)
        if row is None or row.is_deleted:
            raise ProjectNotFoundError("Проект не найден.")
        if (
            row.encrypted_token == project.encrypted_token
            and row.name == project.name
            and (force or row.name == f"VK {row.group_id}")
        ):
            row.name = _validate_name(name)
            session.flush()
        return _to_project(row, cipher)


def update_project(
    project_id: int,
    *,
    name: str | None = None,
    group_id: int | str | None = None,
    token: str | None = None,
    callback_secret: str | None = None,
    confirmation_code: str | None = None,
    video_token: str | None = None,
    enabled: bool | None = None,
) -> Project:
    validate_project_id(project_id)
    with _registry_write() as session:
        cipher = _checked_cipher(session)
        row = session.get(_ProjectRow, project_id)
        if row is None or row.is_deleted:
            raise ProjectNotFoundError("Проект не найден.")
        _to_project(row, cipher)
        if group_id is not None and _validate_group_id(group_id) != row.group_id:
            raise ProjectValidationError(
                "ID сообщества нельзя изменить после создания проекта."
            )
        if name is not None:
            row.name = _validate_name(name)
        if enabled is not None:
            if type(enabled) is not bool:
                raise ProjectValidationError(
                    "Состояние проекта должно быть включено или выключено."
                )
            row.enabled = enabled
        for field_name, value in (
            ("token", token),
            ("secret", callback_secret),
            ("confirmation", confirmation_code),
        ):
            if value is not None:
                value = _validate_credential(value, field_name)
                if not value.strip():
                    continue
                setattr(
                    row,
                    "encrypted_" + field_name,
                    cipher.encrypt(value.encode()).decode(),
                )
        if video_token is not None:
            value = _validate_credential(video_token, "video_token")
            defaults = json.loads(_decrypt(cipher, row.encrypted_defaults))
            defaults["_video_user_token"] = value
            row.encrypted_defaults = cipher.encrypt(
                json.dumps(defaults, ensure_ascii=False).encode()
            ).decode()
        session.flush()
        project = _to_project(row, cipher)
        if project.enabled:
            for field_name in ("token", "callback_secret", "confirmation_code"):
                _validate_credential(
                    getattr(project, field_name), field_name, required=True
                )
    if enabled is False:
        from . import db
        from .waits import cancel_waits

        with project_scope(project), db.SessionLocal() as session:
            cancel_waits(session, "Проект приостановлен")
            session.commit()
    return project


def public_project(project: Project) -> dict:
    """Allowlist for HTTP responses; contains neither plaintext nor ciphertext."""
    return {
        "id": project.id,
        "name": project.name,
        "group_id": project.group_id,
        "enabled": project.enabled,
        "is_legacy": project.is_legacy,
        "is_deleted": project.is_deleted,
        "has_token": bool(project.token),
        "has_secret": bool(project.secret),
        "has_confirmation": bool(project.confirmation),
        "has_video_token": bool(project.video_token),
    }


def delete_project(project_id: int, confirmation: str) -> None:
    """Recoverable removal; never drop schemas/files or delete the VK group."""
    validate_project_id(project_id)
    with _registry_write() as session:
        row = session.get(_ProjectRow, project_id)
        if row is None:
            raise ProjectNotFoundError("Проект не найден.")
        if confirmation.strip() != str(row.group_id):
            raise ProjectValidationError(
                "Для подтверждения введите ID удаляемого сообщества."
            )
        row.enabled, row.is_deleted = False, True
    # From this point API/worker guards reject fresh and stale project contexts.
    _cancel_project_jobs(get_project(project_id, include_deleted=True))


def _cancel_project_jobs(project: Project) -> None:
    from . import db
    from .waits import cancel_waits

    with project_scope(project), db.SessionLocal() as session:
        cancel_waits(session, "Проект удалён")
        session.query(db.Broadcast).filter(
            db.Broadcast.status.in_(["queued", "running", "paused"])
        ).update({"status": "cancelled"})
        cancelled = session.query(db.Broadcast.id).filter_by(status="cancelled")
        session.query(db.BroadcastRecipient).filter(
            db.BroadcastRecipient.broadcast_id.in_(cancelled),
            db.BroadcastRecipient.status == "pending",
        ).update({"status": "cancelled"}, synchronize_session=False)
        session.query(db.Client).update({"profile_requested": False})
        session.commit()


def restore_project(project_id: int) -> Project:
    project = get_project(project_id, include_deleted=True)
    if project is None or not project.is_deleted:
        raise ProjectNotFoundError("Проект не найден в корзине.")
    # Also retries cleanup if a previous removal stopped after registry commit.
    _cancel_project_jobs(project)
    with _registry_write() as session:
        row = session.get(_ProjectRow, project_id)
        if row is None or not row.is_deleted:
            raise ProjectNotFoundError("Проект уже восстановлен.")
        row.is_deleted, row.enabled = False, False
        session.flush()
        return _to_project(row, _checked_cipher(session))


def data_directory(default_path: str | Path) -> Path:
    """Return the legacy path verbatim, or this project's private upload root."""
    project = current_project.get()
    if project is None or project.is_legacy:
        return Path(default_path)
    validate_project_id(project.id)
    path = Path(_setting(_base_settings(), "projects_data_dir", "data/projects")) / str(
        project.id
    )
    path.mkdir(parents=True, exist_ok=True)
    return path
