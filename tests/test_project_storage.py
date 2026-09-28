"""Storage-only tests; no app startup or legacy test setup monkeypatches.

PROJECT_TEST_DATABASE_URL optionally names a PostgreSQL maintenance database on
a disposable test server. Each case creates and drops its own uniquely named DB;
it never creates tables in the maintenance database or an existing application DB.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app import config


@pytest.fixture(params=["sqlite", "postgresql"])
def storage(request, monkeypatch, tmp_path):
    maintenance = None
    database_name = None
    if request.param == "postgresql":
        supplied_url = os.environ.get("PROJECT_TEST_DATABASE_URL")
        if not supplied_url:
            pytest.skip("PROJECT_TEST_DATABASE_URL is not configured")
        maintenance = create_engine(
            supplied_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        database_name = "project_storage_" + uuid4().hex
        with maintenance.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = make_url(supplied_url).set(database=database_name)
        engine = create_engine(url, poolclass=NullPool)
    else:
        url = make_url("sqlite://").set(database=str(tmp_path / "legacy.db"))
        engine = create_engine(url, connect_args={"check_same_thread": False})
    base = config.Settings.model_construct(
        database_url=url.render_as_string(hide_password=False),
        vk_group_id=123,
        vk_group_token="legacy-token-KEEP",
        vk_callback_secret="legacy-secret-KEEP",
        vk_confirmation_code="legacy-confirm-KEEP",
        admin_username="admin",
        admin_password="owner-password",
        admin_session_secret="session-is-not-encryption",
        projects_encryption_key="",
        projects_key_file=str(tmp_path / "projects.key"),
        projects_data_dir=str(tmp_path / "project-files"),
        operator_user_id="9001",
        chat_greeting="legacy-specific-greeting",
        test_mode="true",
        promo_code="legacy-promo-code",
        background_jobs_enabled=False,
    )
    monkeypatch.setattr(config, "get_base_settings", lambda: base)
    monkeypatch.setenv("PROJECTS_DATA_DIR", str(tmp_path / "project-files"))
    from app import db, projects

    monkeypatch.setattr(db, "get_base_settings", lambda: base)
    monkeypatch.setattr(db, "engine", engine)
    try:
        with projects.project_scope(None):
            db.init_db()
            yield SimpleNamespace(
                db=db, projects=projects, base=base, engine=engine, path=tmp_path
            )
    finally:
        db.dispose_project_engines()
        engine.dispose()
        if maintenance is not None:
            with maintenance.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}"'))
            maintenance.dispose()


def create_other(storage, group=456, **kwargs):
    return storage.projects.create_project(
        "Other group",
        str(group),
        "other-token-PRIVATE",
        "other-secret-PRIVATE",
        "other-confirm-PRIVATE",
        **kwargs,
    )


def seed_models(storage, label):
    db = storage.db
    with db.SessionLocal() as session:
        session.add_all(
            [
                db.Campaign(id=42, post_id=594, title=label),
                db.Scenario(id=51, title=label, draft={"name": label}),
                db.Conversation(user_id=77, scenario_id=51, handoff=True),
                db.PendingGift(
                    id="same-gift-key",
                    user_id=77,
                    campaign_id=42,
                    event_key="same-event-key",
                    active_key="same-active-key",
                ),
                db.DialogEvent(
                    id=31, event_key="same-dialog-key", user_id=77, text=label
                ),
                db.ProcessedComment(
                    id=22,
                    event_key="wall:-123:594:7",
                    owner_id=-123,
                    comment_id=7,
                    post_id=594,
                    user_id=77,
                    campaign_id=42,
                ),
                db.Client(user_id=77, first_name=label),
                db.BotSetting(key="operator_user_id", value="override-" + label),
                db.MediaAsset(
                    id="same-media-key",
                    filename="asset.png",
                    content_type="image/png",
                    path=label,
                ),
                db.PromoDelivery(id=61, user_id=77, campaign_id=42),
                db.BotMessage(event_key="same-bot-message-key", user_id=77),
                db.Broadcast(id="same-broadcast-key", title=label, message=label),
                db.BroadcastRecipient(
                    id=71, broadcast_id="same-broadcast-key", user_id=77
                ),
                db.WorkLease(name="same-worker-key", owner=label),
            ]
        )
        session.commit()


def model_snapshot(storage, engine):
    with engine.connect() as connection:
        return {
            table.name: list(connection.execute(select(table)).mappings())
            for table in storage.db.Base.metadata.sorted_tables
        }


def test_legacy_bootstrap_preserves_every_model_and_snapshot_defaults(storage):
    db, projects = storage.db, storage.projects
    seed_models(storage, "legacy")
    before = model_snapshot(storage, storage.engine)
    (legacy,) = projects.init_registry()
    assert isinstance(legacy.id, int) and legacy.is_legacy and legacy.enabled
    assert db.get_project_engine(legacy) is storage.engine
    assert projects.get_project(legacy.id).token == "legacy-token-KEEP"
    assert projects.get_project_by_group_id("123").id == legacy.id
    storage.base.vk_group_id = 0
    storage.base.vk_group_token = ""
    storage.base.vk_callback_secret = ""
    storage.base.vk_confirmation_code = ""
    storage.base.operator_user_id = ""
    storage.base.chat_greeting = "changed-env-greeting"
    storage.base.test_mode = "false"
    assert projects.init_registry()[0].id == legacy.id
    with projects.project_scope(projects.get_project(legacy.id)):
        settings = config.get_settings()
        assert settings.vk_group_id == 123
        assert settings.vk_group_token == "legacy-token-KEEP"
        assert settings.operator_user_id == "9001"
        assert settings.chat_greeting == "legacy-specific-greeting"
        assert settings.test_mode == "true"
        with db.SessionLocal() as session:
            assert db.read_settings(session)["operator_user_id"] == "override-legacy"
    assert model_snapshot(storage, storage.engine) == before


def test_new_project_isolates_every_model_and_registry_metadata(storage):
    db, projects = storage.db, storage.projects
    (legacy,) = projects.init_registry()
    seed_models(storage, "legacy")
    before = model_snapshot(storage, storage.engine)
    other = create_other(storage)
    isolated = db.get_project_engine(other)
    assert isolated is not storage.engine
    assert isinstance(isolated.pool, NullPool)
    with projects.project_scope(other):
        assert projects.list_projects()[0].id == legacy.id
        with db.SessionLocal() as session:
            for table in db.Base.metadata.sorted_tables:
                assert not list(session.execute(select(table)))
        seed_models(storage, "other")
        with db.SessionLocal() as session:
            assert session.get(db.Campaign, 42).title == "other"
            assert session.get(db.Client, 77).first_name == "other"
            assert session.get(db.WorkLease, "same-worker-key").owner == "other"
    assert model_snapshot(storage, storage.engine) == before
    assert set(projects.RegistryBase.metadata.tables).isdisjoint(
        db.Base.metadata.tables
    )
    assert "projects" in inspect(storage.engine).get_table_names()
    assert "projects" not in inspect(isolated).get_table_names()
    if storage.engine.dialect.name == "sqlite":
        assert (
            Path(isolated.url.database).parent
            == Path(storage.engine.url.database).parent
        )
        assert Path(isolated.url.database).name == f"project_{other.id}.db"
    projects.init_registry()
    assert model_snapshot(storage, storage.engine) == before
    with projects.project_scope(other), db.SessionLocal() as session:
        assert session.get(db.Client, 77).first_name == "other"


def test_new_settings_use_class_defaults_and_keep_owner_infrastructure(storage):
    projects = storage.projects
    (legacy,) = projects.init_registry()
    other = create_other(storage)
    with projects.project_scope(other):
        settings = config.get_settings()
        assert settings.operator_user_id == ""
        assert settings.test_mode == "false"
        assert (
            settings.chat_greeting
            == config.Settings.model_fields["chat_greeting"].default
        )
        assert settings.promo_code == "WELCOME"
        assert settings.vk_group_id == 456 and settings.vk_group_token == other.token
        for field in (
            "database_url",
            "admin_username",
            "admin_password",
            "admin_session_secret",
        ):
            assert getattr(settings, field) == getattr(storage.base, field)
        settings.operator_user_id = "a-local-edit"
        assert config.get_settings().operator_user_id == ""
        with projects.project_scope(legacy):
            assert config.get_settings().operator_user_id == "9001"
        assert config.get_settings().operator_user_id == ""
    assert config.get_settings() is storage.base


def test_empty_install_bootstrap_once_and_first_ui_project_never_adopts_legacy(storage):
    projects = storage.projects
    storage.base.vk_group_id = 0
    storage.base.vk_group_token = ""
    assert projects.init_registry() == []
    assert projects.list_projects() == []
    assert projects.current_project.get() is None
    storage.base.vk_group_id = 123
    storage.base.vk_group_token = "later-ENV-must-not-import"
    assert projects.init_registry() == []
    project = projects.create_project("Draft", "123")
    assert not project.is_legacy and not project.enabled and project.token == ""
    assert storage.db.get_project_engine(project) is not storage.engine
    assert projects.init_registry()[0].id == project.id


def test_credentials_encrypted_masked_and_key_independent_from_session_secret(storage):
    projects = storage.projects
    (legacy,) = projects.init_registry()
    key_file = Path(storage.base.projects_key_file)
    original_key = key_file.read_bytes()
    with storage.engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM projects")).mappings().all()
    raw = str(rows)
    public = json.dumps(projects.public_project(legacy))
    for value in (
        legacy.token,
        legacy.secret,
        legacy.confirmation,
        "legacy-specific-greeting",
    ):
        assert value not in raw and value not in repr(legacy) and value not in public
    assert projects.public_project(legacy)["has_token"]
    assert "encrypted_token" not in public
    storage.base.admin_session_secret = "rotated-session-secret"
    assert projects.get_project(legacy.id).token == legacy.token
    assert projects.init_registry()[0].id == legacy.id
    assert key_file.read_bytes() == original_key


@pytest.mark.parametrize("failure", ["missing", "wrong", "invalid", "ciphertext"])
def test_key_and_ciphertext_fail_closed_without_regeneration(storage, failure):
    projects = storage.projects
    (legacy,) = projects.init_registry()
    key_file = Path(storage.base.projects_key_file)
    key = key_file.read_bytes()
    if failure == "missing":
        key_file.unlink()
    elif failure == "wrong":
        storage.base.projects_encryption_key = Fernet.generate_key().decode()
    elif failure == "invalid":
        storage.base.projects_encryption_key = "not-a-key"
    else:
        with storage.engine.begin() as connection:
            connection.execute(text("UPDATE projects SET encrypted_secret = 'broken'"))
    with pytest.raises(projects.ProjectEncryptionError):
        projects.init_registry()
    with pytest.raises(projects.ProjectEncryptionError):
        projects.get_project(legacy.id)
    if failure == "missing":
        assert not key_file.exists()
        storage.base.projects_encryption_key = key.strip().decode()
        assert projects.init_registry()[0].token == legacy.token
    else:
        assert key_file.read_bytes() == key


def test_explicit_encryption_key_does_not_create_a_file(storage):
    storage.base.projects_encryption_key = Fernet.generate_key().decode()
    (legacy,) = storage.projects.init_registry()
    assert not Path(storage.base.projects_key_file).exists()
    assert storage.projects.get_project(legacy.id).token == legacy.token


def test_crud_validation_immutability_drafts_and_blank_secret_retention(storage):
    projects = storage.projects
    projects.init_registry()
    other = projects.create_project("Draft", "456")
    assert not other.enabled
    with pytest.raises(ValueError, match="Токен"):
        projects.update_project(other.id, enabled=True)
    with pytest.raises(ValueError, match="нельзя изменить"):
        projects.update_project(other.id, group_id=789)
    for invalid in (0, -1, True, "1; DROP TABLE projects", "-123", 1.5, "9" * 30):
        with pytest.raises(ValueError):
            projects.create_project("Invalid", invalid)
    with pytest.raises(ValueError, match="уже существует"):
        create_other(storage)
    for missing in ("callback_secret", "confirmation_code"):
        args = {
            "token": "draft-token",
            "callback_secret": "draft-secret",
            "confirmation_code": "draft-confirm",
        }
        args[missing] = ""
        with pytest.raises(ValueError, match="заполните поле"):
            projects.update_project(other.id, enabled=True, **args)
    other = projects.update_project(
        other.id,
        name="Ready",
        group_id="456",
        token="new-token",
        callback_secret="new-secret",
        confirmation_code="new-confirm",
        enabled=True,
    )
    assert other.enabled
    other = projects.update_project(
        other.id, token="", callback_secret="  ", confirmation_code=None
    )
    assert (other.token, other.secret, other.confirmation) == (
        "new-token",
        "new-secret",
        "new-confirm",
    )
    projects.update_project(other.id, enabled=False)
    assert len(projects.list_projects()) == 2
    assert len(projects.list_projects(enabled_only=True)) == 1
    assert projects.get_project(999999) is None
    assert projects.get_project_by_group_id(999999) is None
    with pytest.raises(ValueError):
        projects.update_project(999999, name="Missing")


def test_duplicate_group_creation_is_atomic(storage):
    projects = storage.projects
    projects.init_registry()

    def create():
        try:
            return create_other(storage)
        except projects.ProjectValidationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(projects.list_projects()) == 2


def test_context_isolation_tasks_thread_offload_raw_executor_and_existing_sessions(
    storage,
):
    db, projects = storage.db, storage.projects
    (legacy,) = projects.init_registry()
    other = create_other(storage)
    for project in (legacy, other):
        with projects.project_scope(project), db.SessionLocal() as session:
            db.save_settings(session, {"project-owner": str(project.id)})
    base_engine = db.engine

    def read():
        project = projects.current_project.get()
        with db.SessionLocal() as session:
            assert db.read_settings(session)["project-owner"] == str(project.id)
        return config.get_settings().vk_group_id

    async def worker():
        await asyncio.sleep(0)
        expected = projects.current_project.get().group_id
        assert await asyncio.to_thread(read) == expected
        assert (
            await asyncio.get_running_loop().run_in_executor(
                None, projects.bind_project_context(read)
            )
            == expected
        )
        return read()

    async def run():
        with projects.project_scope(legacy):
            first = asyncio.create_task(worker())
        with projects.project_scope(other):
            second = asyncio.create_task(worker())
        assert projects.current_project.get() is None
        assert await asyncio.gather(first, second) == [123, 456]

    asyncio.run(run())
    with projects.project_scope(other):
        session = db.SessionLocal()
        with projects.project_scope(None):
            assert db.current_engine() is base_engine
        assert db.read_settings(session)["project-owner"] == str(other.id)
    try:
        assert db.read_settings(session)["project-owner"] == str(other.id)
    finally:
        session.close()
    with pytest.raises(RuntimeError), projects.project_scope(legacy):
        raise RuntimeError("scope must reset on failure")
    assert projects.current_project.get() is None
    assert db.engine is base_engine
    with db.SessionLocal() as session:
        assert session.get_bind() is base_engine


def test_upload_paths_keep_legacy_and_isolate_equal_filenames(storage):
    projects = storage.projects
    (legacy,) = projects.init_registry()
    first = create_other(storage)
    second = create_other(storage, group=789)
    original = storage.path / "legacy-uploads"
    original.mkdir()
    (original / "same.png").write_bytes(b"legacy")
    assert projects.data_directory(original) == original
    with projects.project_scope(legacy):
        assert projects.data_directory(original) == original
    for project in (first, second):
        with projects.project_scope(project):
            directory = projects.data_directory(original)
            assert directory == storage.path / "project-files" / str(project.id)
            (directory / "same.png").write_bytes(str(project.id).encode())
    assert (original / "same.png").read_bytes() == b"legacy"


def create_comment_archive(engine, comment_id):
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE processed_comments (id INTEGER PRIMARY KEY, comment_id INTEGER UNIQUE, "
                "post_id INTEGER, user_id INTEGER, status VARCHAR(32), error TEXT, created_at TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO processed_comments VALUES (1, :comment_id, 594, 77, 'sent', NULL, CURRENT_TIMESTAMP)"
            ),
            {"comment_id": comment_id},
        )


def test_raw_additive_migrations_and_comment_owner_only_touch_target(storage):
    db, projects = storage.db, storage.projects
    (legacy,) = projects.init_registry()
    other = create_other(storage)
    isolated = db.get_project_engine(other)
    create_comment_archive(storage.engine, 7)
    create_comment_archive(isolated, 8)
    for engine in (storage.engine, isolated):
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE campaigns DROP COLUMN attachment_name")
            )
            connection.execute(text("ALTER TABLE campaigns DROP COLUMN plus_words"))
    db.init_db(isolated, migration_owner_group_id=456)
    assert "attachment_name" in {
        column["name"] for column in inspect(isolated).get_columns("campaigns")
    }
    assert "plus_words" in {
        column["name"] for column in inspect(isolated).get_columns("campaigns")
    }
    assert "attachment_name" not in {
        column["name"] for column in inspect(storage.engine).get_columns("campaigns")
    }
    with projects.project_scope(other), db.SessionLocal() as session:
        assert session.query(db.ProcessedComment).one().event_key == "wall:-456:594:8"
    with db.SessionLocal() as session:
        assert session.query(db.ProcessedComment).count() == 0
    storage.base.vk_group_id = 999
    projects.init_registry()
    with projects.project_scope(legacy), db.SessionLocal() as session:
        assert session.query(db.ProcessedComment).one().event_key == "wall:-123:594:7"
    projects.init_registry()
    with projects.project_scope(other), db.SessionLocal() as session:
        assert session.query(db.ProcessedComment).count() == 1


def test_missing_project_table_cannot_read_legacy_and_reinit_recreates_it(storage):
    db, projects = storage.db, storage.projects
    projects.init_registry()
    with db.SessionLocal() as session:
        db.save_settings(session, {"private-legacy-setting": "must-not-leak"})
    other = create_other(storage)
    isolated = db.get_project_engine(other)
    with isolated.begin() as connection:
        connection.execute(text("DROP TABLE bot_settings"))
    with isolated.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(text("SELECT * FROM bot_settings"))
    with projects.project_scope(other):
        db.init_db()
        with db.SessionLocal() as session:
            assert db.read_settings(session) == {}
    with db.SessionLocal() as session:
        assert db.read_settings(session) == {"private-legacy-setting": "must-not-leak"}
    if storage.engine.dialect.name == "postgresql":
        with isolated.connect() as connection:
            assert connection.scalar(text("SHOW search_path")) == f"project_{other.id}"
            assert (
                connection.scalar(text("SELECT current_schema()"))
                == f"project_{other.id}"
            )
            assert connection.scalar(text("SELECT to_regclass('projects')")) is None
        assert inspect(isolated).default_schema_name == f"project_{other.id}"
        with storage.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT to_regclass('public.projects')"))
                is not None
            )


def test_replacing_default_engine_has_independent_registry_and_engine_cache(
    storage, monkeypatch
):
    # This explicitly exercises the old tests' db.engine replacement mechanism.
    if storage.engine.dialect.name != "sqlite":
        pytest.skip("SQLite engine replacement is the legacy test fixture contract")
    db, projects = storage.db, storage.projects
    projects.init_registry()
    first = create_other(storage)
    first_engine = db.get_project_engine(first)
    replacement = create_engine("sqlite://", connect_args={"check_same_thread": False})
    try:
        monkeypatch.setattr(db, "engine", replacement)
        (replacement_legacy,) = projects.init_registry()
        assert replacement_legacy.group_id == 123
        second = create_other(storage)
        assert first.id == second.id
        assert db.get_project_engine(second) is not first_engine
        assert len(projects.list_projects()) == 2
    finally:
        replacement.dispose()


def test_missing_env_cannot_orphan_existing_legacy_data(storage):
    projects = storage.projects
    seed_models(storage, "preserve-this")
    before = model_snapshot(storage, storage.engine)
    storage.base.vk_group_token = ""
    with pytest.raises(ValueError, match="VK_GROUP_ID.*VK_GROUP_TOKEN"):
        projects.init_registry()
    assert model_snapshot(storage, storage.engine) == before
    assert not Path(storage.base.projects_key_file).exists()
    storage.base.vk_group_token = "restored-original-token"
    (legacy,) = projects.init_registry()
    assert legacy.is_legacy
    assert model_snapshot(storage, storage.engine) == before


def test_legacy_archive_alone_blocks_empty_bootstrap(storage):
    projects = storage.projects
    create_comment_archive(storage.engine, 7)
    storage.base.vk_group_id = 0
    storage.base.vk_group_token = ""
    with pytest.raises(ValueError, match="исходной базе"):
        projects.init_registry()
    storage.base.vk_group_id = 123
    storage.base.vk_group_token = "restored-original-token"
    (legacy,) = projects.init_registry()
    with projects.project_scope(legacy), storage.db.SessionLocal() as session:
        assert (
            session.query(storage.db.ProcessedComment).one().event_key
            == "wall:-123:594:7"
        )


def test_unicode_credentials_normalization_limits_and_legacy_keys_unchanged(storage):
    projects = storage.projects
    storage.base.vk_callback_secret = "  прежний ключ 🔑  "
    (legacy,) = projects.init_registry()
    assert legacy.callback_secret == "  прежний ключ 🔑  "
    project = projects.create_project(
        "  UTF-8  ",
        " 456 ",
        "  токен 🔑  ",
        " секрет ",
        " код ",
        enabled=True,
    )
    assert project.name == "UTF-8"
    assert (project.token, project.secret, project.confirmation) == (
        "токен 🔑",
        "секрет",
        "код",
    )
    updated = projects.update_project(project.id, callback_secret=" новый секрет ✓ ")
    assert updated.callback_secret == "новый секрет ✓"
    for field, value in (
        ("token", "x" * 4097),
        ("callback_secret", "x" * 1025),
        ("confirmation_code", "x" * 1025),
        ("token", "\ud800"),
    ):
        with pytest.raises(projects.ProjectValidationError):
            projects.update_project(project.id, **{field: value})
    assert projects.get_project(project.id).callback_secret == "новый секрет ✓"


def test_trash_migrations_preserve_rows_and_restore_without_resending(storage):
    from app import recycle

    db, projects = storage.db, storage.projects
    (legacy,) = projects.init_registry()
    seed_models(storage, "history-before-trash")
    before = model_snapshot(storage, storage.engine)
    # Simulate the previous deployment, on this fixture's disposable DB only.
    with storage.engine.begin() as connection:
        for table, column in (
            ("projects", "is_deleted"),
            ("campaigns", "is_deleted"),
            ("campaigns", "archived_post_id"),
            ("scenarios", "is_deleted"),
            ("broadcasts", "is_deleted"),
        ):
            connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    (legacy,) = projects.init_registry()
    assert model_snapshot(storage, storage.engine) == before
    with projects.project_scope(legacy):
        with db.SessionLocal() as session:
            recycle.remove_campaign(session, session.get(db.Campaign, 42))
            recycle.remove_broadcast(
                session, session.get(db.Broadcast, "same-broadcast-key")
            )
            session.commit()
            assert session.get(db.Campaign, 42).post_id == -42
            assert session.get(db.PendingGift, "same-gift-key").status == "cancelled"
        recycle.restore("campaign", "42")
        recycle.restore("broadcast", "same-broadcast-key")
        with db.SessionLocal() as session:
            campaign = session.get(db.Campaign, 42)
            assert campaign.post_id == 594 and not campaign.enabled
            assert not campaign.is_deleted
            assert session.get(db.Broadcast, "same-broadcast-key").status == "cancelled"
            assert session.get(db.PromoDelivery, 61).campaign_id == 42
            assert session.get(db.DialogEvent, 31).text == "history-before-trash"
    other = create_other(storage)
    projects.delete_project(legacy.id, str(legacy.group_id))
    assert [p.id for p in projects.init_registry()] == [other.id]
    restored = projects.restore_project(legacy.id)
    assert not restored.enabled and not restored.is_deleted
    assert restored.token == legacy.token
