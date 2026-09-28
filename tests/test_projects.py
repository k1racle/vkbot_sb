"""HTTP integration coverage for project isolation, using the real scoped sessions.

No shared setup fixture: rebinding SessionLocal would hide the isolation under test.
All VK calls run through the production client and stop at a fake HTTP transport.
"""

import asyncio
import base64
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import weakref

# Protect standalone collection before app.db creates its initial engine.
os.environ.update(
    VK_GROUP_ID="123",
    VK_GROUP_TOKEN="test-only",
    VK_CALLBACK_SECRET="test-secret",
    VK_CONFIRMATION_CODE="test-code",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="test-password",
    ADMIN_SESSION_SECRET="test-session-secret",
    DATABASE_URL="sqlite://",
    CHAT_ENABLED="true",
    BACKGROUND_JOBS_ENABLED="false",
)

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import broadcasts, config, db, dialog, main, projects, vk_api


class ProjectTestSettings(config.Settings):
    projects_data_dir: str


@pytest.fixture
def project_env(monkeypatch, tmp_path, request):
    defaults = {
        name: field.get_default(call_default_factory=True)
        for name, field in config.Settings.model_fields.items()
        if not field.is_required()
    }
    defaults.update(
        vk_group_id=123,
        vk_group_token="legacy-token-project-test",
        vk_callback_secret="legacy-secret-project-test",
        vk_confirmation_code="legacy-confirmation-project-test",
        admin_username="admin",
        admin_password="test-password",
        admin_session_secret="test-session-secret",
        database_url=f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}",
        projects_encryption_key="",
        projects_key_file=str(tmp_path / "projects.key"),
        projects_data_dir=str(tmp_path / "uploads"),
        background_jobs_enabled=False,
        operator_user_id="99",
        chat_greeting="Legacy ENV greeting",
    )
    defaults.update(getattr(request, "param", {}))
    base = ProjectTestSettings(_env_file=None, **defaults)
    monkeypatch.setattr(config, "get_base_settings", lambda: base)
    monkeypatch.setenv("PROJECTS_DATA_DIR", base.projects_data_dir)
    engine = create_engine(base.database_url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "_project_engines", weakref.WeakKeyDictionary())
    monkeypatch.setattr(dialog, "_locks", weakref.WeakValueDictionary())
    slots = asyncio.Semaphore(4)
    monkeypatch.setattr(dialog, "CALLBACK_SLOTS", slots)
    monkeypatch.setattr(main, "CALLBACK_SLOTS", slots)
    monkeypatch.setattr(main, "ATTACHMENT_DIR", tmp_path)
    scope_token = projects.current_project.set(None)
    network = SimpleNamespace(calls=[], replies={})

    async def fake_vk_http(_transport, request):
        # Unknown requests fail closed; never fall back to a real network call.
        assert request.url.host == "api.vk.com", f"Unexpected network: {request.url}"
        assert request.method == "POST"
        params = {
            key: values[-1]
            for key, values in parse_qs((await request.aread()).decode()).items()
        }
        method = request.url.path.rsplit("/", 1)[-1]
        selected = projects.current_project.get()
        settings = config.get_settings()
        call = {
            "method": method,
            "params": params,
            "headers": dict(request.headers),
            "project_id": selected.id if selected else None,
            "group_id": settings.vk_group_id,
            "token": settings.vk_group_token,
        }
        network.calls.append(call)
        await asyncio.sleep(0)  # Force requests from different scopes to overlap.
        assert config.get_settings().vk_group_token == call["token"]
        assert config.get_settings().vk_group_id == call["group_id"]
        assert params["access_token"] == (
            projects.get_project(selected.id).video_token
            if method == "video.createComment"
            else call["token"]
        )
        if method in network.replies:
            payload = network.replies[method]
        elif method == "users.get":
            payload = {
                "response": [
                    {
                        "id": int(user),
                        "first_name": "Test",
                        "last_name": str(call["group_id"]),
                    }
                    for user in params["user_ids"].split(",")
                ]
            }
        elif method == "groups.getById":
            owner = next(
                p for p in projects.list_projects() if p.token == params["access_token"]
            )
            payload = {"response": {"groups": [{"id": owner.group_id}]}}
        elif method == "groups.isMember":
            payload = {"response": 1}
        elif method == "messages.isMessagesFromGroupAllowed":
            payload = {"response": {"is_allowed": 1}}
        elif method in {"messages.send", "wall.createComment", "video.createComment"}:
            payload = {"response": len(network.calls)}
        else:
            raise AssertionError(f"Unmocked VK method: {method}")
        return httpx.Response(200, json=payload, request=request)

    def reject_sync_http(_transport, request):
        raise AssertionError(f"Unexpected synchronous network request: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_vk_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_sync_http)
    try:
        db.init_db()
        if base.vk_group_id and base.vk_group_token:
            with db.SessionLocal() as session:
                db.save_settings(session, {"chat_greeting": "Legacy saved greeting"})
        # Startup initializes the registry, migrations and legacy backfill normally.
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client, base=base, network=network, engine=engine, root=tmp_path
            )
    finally:
        db.dispose_project_engines()
        engine.dispose()
        projects.current_project.reset(scope_token)


def login(client):
    result = client.post(
        "/login", data={"username": "admin", "password": "test-password"}
    )
    assert result.status_code == 200, result.text
    assert result.url.path == "/projects"
    match = re.search(r'name="csrf-token" content="([^"]+)"', result.text)
    assert match, "The projects page must provide CSRF for all admin POSTs"
    client.headers["X-CSRF-Token"] = match[1]
    return match[1]


def prefix(project):
    return f"/p/{project.id}/admin"


def credentials(group_id):
    return {
        "token": f"private-token-{group_id}-project-test",
        "callback_secret": f"private-secret-{group_id}-project-test",
        "confirmation_code": f"private-confirmation-{group_id}-project-test",
    }


def create(client, name="Alpha", group_id=456):
    response = client.post(
        "/projects",
        data={"name": name, "group_id": str(group_id), **credentials(group_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    project = projects.get_project_by_group_id(group_id)
    assert project is not None
    assert response.headers["location"] == f"/projects#project-{project.id}"
    return project


def configure(client, project, *, enabled=True, **fields):
    data = {
        "name": project.name,
        "token": "",
        "callback_secret": "",
        "confirmation_code": "",
    }
    if enabled:
        data["enabled"] = "1"
    data.update(fields)
    response = client.post(
        f"/projects/{project.id}/settings", data=data, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return projects.get_project(project.id)


@pytest.fixture
def two_projects(project_env):
    login(project_env.client)
    first = configure(project_env.client, create(project_env.client))
    second = configure(project_env.client, create(project_env.client, "Beta", 789))
    return project_env, first, second


@contextmanager
def session_for(project):
    with projects.project_scope(project), db.SessionLocal() as session:
        yield session


def message(project, text="hello", number=1, user=77):
    return {
        "type": "message_new",
        "group_id": project.group_id,
        "secret": project.secret,
        "event_id": f"same-event-{user}-{number}",
        "object": {
            "message": {
                "from_id": user,
                "peer_id": user,
                "conversation_message_id": number,
                "text": text,
            }
        },
    }


def comment(project, number=1):
    return {
        "type": "wall_reply_new",
        "group_id": project.group_id,
        "secret": project.secret,
        "event_id": f"same-comment-{number}",
        "object": {
            "id": number,
            "post_id": 55,
            "owner_id": -project.group_id,
            "from_id": 77,
            "text": "Please send a promo",
        },
    }


def callback(client, project, payload):
    return client.post(f"/vk/callback/{project.id}", json=payload)


def campaign(client, project, code):
    result = client.post(
        prefix(project) + "/campaigns",
        data={
            "title": code,
            "post_id": "55",
            "promo_code": code,
            "promo_message": "Promo {promo_code}",
            "shop_url": "https://example.com",
            "enabled": "1",
            "one_promo_per_user": "1",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    assert result.headers["location"].startswith(prefix(project) + "?")
    assert "campaign_saved=1" in result.headers["location"]
    return int(parse_qs(result.headers["location"].split("?", 1)[1])["campaign_id"][0])


def scenario(client, project, title):
    route = prefix(project) + "/api/scenarios"
    response = client.post(route)
    assert response.status_code == 200, response.text
    row = response.json()
    graph = {
        "nodes": [
            {"id": "start", "type": "start", "next": "answer"},
            {
                "id": "answer",
                "type": "question",
                "text": title,
                "variable": "reply",
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ]
    }
    response = client.put(
        f"{route}/{row['id']}",
        json={
            "title": title,
            "graph": graph,
            "revision": row["revision"],
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()
    response = client.post(
        f"{route}/{row['id']}/publish", json={"revision": row["revision"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def queued_broadcast(client, project, title):
    response = client.post(
        prefix(project) + "/api/broadcasts",
        json={
            "title": title,
            "message": title,
            "audience": "selected",
            "user_ids": [77],
        },
    )
    assert response.status_code == 200, response.text
    job = response.json()
    response = client.post(
        prefix(project) + f"/api/broadcasts/{job['id']}/start",
        json={
            "confirm_consent": True,
            "expected_count": job["total"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    return job


def test_bootstrap_keeps_legacy_storage_and_saved_defaults_once(
    project_env, monkeypatch
):
    env = project_env
    (legacy,) = projects.list_projects()
    assert legacy.is_legacy and legacy.enabled and legacy.group_id == 123
    with session_for(legacy) as session:
        assert session.bind is env.engine
        assert db.read_settings(session)["chat_greeting"] == "Legacy saved greeting"
    assert Path(env.base.projects_key_file).is_file()
    key_before = Path(env.base.projects_key_file).read_bytes()
    changed = env.base.model_copy(
        update={
            "vk_group_id": 999,
            "vk_group_token": "different-env-token",
            "vk_callback_secret": "different-env-secret",
            "chat_greeting": "Changed ENV",
        }
    )
    monkeypatch.setattr(config, "get_base_settings", lambda: changed)
    projects.init_registry()
    (again,) = projects.list_projects()
    assert (again.id, again.group_id, again.token, again.secret) == (
        legacy.id,
        123,
        legacy.token,
        legacy.secret,
    )
    assert Path(env.base.projects_key_file).read_bytes() == key_before
    with projects.project_scope(again):
        assert config.get_settings().chat_greeting == "Legacy ENV greeting"
        assert config.get_settings().operator_user_id == "99"
    login(env.client)
    response = env.client.post("/vk/callback", json=message(again))
    assert response.text == "ok"
    (sent,) = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert sent["params"]["message"] == "Legacy saved greeting"
    assert env.client.get("/admin/api/clients/77").status_code == 200
    assert env.client.get("/admin/api/conversations").json()[0]["user_id"] == 77


def test_new_projects_start_paused_and_do_not_inherit_legacy_defaults(project_env):
    env = project_env
    login(env.client)
    first, second = create(env.client), create(env.client, "Beta", 789)
    assert first.id != second.id
    for project in (first, second):
        assert not project.enabled and not project.is_legacy
        with projects.project_scope(project):
            settings = config.get_settings()
            assert settings.vk_group_id == project.group_id
            assert settings.operator_user_id == ""
            assert settings.chat_greeting != env.base.chat_greeting
        assert callback(env.client, project, message(project)).text == "ok"
        with session_for(project) as session:
            assert session.bind is not env.engine
            assert (
                Path(session.bind.url.database).resolve().parent == env.root.resolve()
            )
            assert session.query(db.Client).count() == 0
            assert session.query(db.DialogEvent).count() == 0
            assert "chat_greeting" not in db.read_settings(session)
    assert not env.network.calls
    with env.engine.connect() as connection:
        stored = str(connection.execute(text("SELECT * FROM projects")).all())
    for project in (first, second):
        for value in (project.token, project.secret, project.confirmation):
            assert value not in stored
    assert db.get_project_engine(first) is not db.get_project_engine(second)


def test_settings_blank_fields_retain_credentials_and_rotation_is_local(two_projects):
    env, first, second = two_projects
    retained = configure(
        env.client, first, name="Renamed", token="  ", callback_secret="  "
    )
    assert retained.name == "Renamed" and retained.enabled
    assert (retained.token, retained.secret, retained.confirmation) == (
        first.token,
        first.secret,
        first.confirmation,
    )
    rotated = configure(
        env.client,
        retained,
        token="rotated-token",
        callback_secret="rotated-secret",
        confirmation_code="rotated-confirmation",
    )
    assert callback(env.client, rotated, message(first)).text == "invalid secret"
    assert callback(env.client, rotated, message(rotated)).text == "ok"
    other = projects.get_project(second.id)
    assert (other.token, other.secret, other.confirmation) == (
        second.token,
        second.secret,
        second.confirmation,
    )
    assert {c["params"]["access_token"] for c in env.network.calls} == {rotated.token}


@pytest.mark.parametrize(
    "group_id", ["0", "-123", "123.5", "１２３", "abc", "123", "999999999999999999999"]
)
def test_invalid_or_duplicate_group_creation_is_atomic(project_env, group_id):
    login(project_env.client)
    before = [(p.id, p.group_id, p.token) for p in projects.list_projects()]
    response = project_env.client.post(
        "/projects",
        data={
            "name": "Rejected",
            "group_id": group_id,
            **credentials(456),
        },
    )
    assert response.status_code == 422
    assert [(p.id, p.group_id, p.token) for p in projects.list_projects()] == before
    assert not project_env.network.calls


def test_authentication_is_required_before_project_data_or_credentials(project_env):
    env = project_env
    (legacy,) = projects.list_projects()
    for path in ("/projects", prefix(legacy)):
        response = env.client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    for suffix in (
        "scenarios",
        "clients",
        "conversations",
        "broadcasts",
        "media/unknown",
    ):
        assert env.client.get(prefix(legacy) + "/api/" + suffix).status_code == 401
    for path in (
        "/projects",
        f"/projects/{legacy.id}/settings",
        f"/projects/{legacy.id}/check",
    ):
        response = env.client.post(
            path, data={"name": "Intruder", "enabled": "1"}, follow_redirects=False
        )
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert len(projects.list_projects()) == 1
    assert projects.get_project(legacy.id).name == legacy.name
    assert not env.network.calls


@pytest.mark.parametrize("csrf", ["", "incorrect-session-token"])
def test_csrf_rejects_project_forms_and_existing_admin_mutations(two_projects, csrf):
    env, first, _ = two_projects
    scenario_row = scenario(env.client, first, "Unchanged")
    campaign_id = campaign(env.client, first, "UNCHANGED")
    env.client.headers["X-CSRF-Token"] = csrf
    requests = [
        ("POST", "/projects", {"data": {"name": "Forbidden", "group_id": "999"}}),
        ("POST", f"/projects/{first.id}/settings", {"data": {"name": "Forbidden"}}),
        ("POST", f"/projects/{first.id}/check", {}),
        ("POST", prefix(first) + "/settings", {"data": {"test_mode": "1"}}),
        (
            "POST",
            prefix(first) + "/chat-settings",
            {"data": {"chat_greeting": "Forbidden"}},
        ),
        ("POST", prefix(first) + f"/campaigns/{campaign_id}/delete", {}),
        (
            "POST",
            prefix(first) + "/test-send",
            {"data": {"campaign_id": campaign_id, "user_id": 77}},
        ),
        ("POST", prefix(first) + "/api/scenarios", {}),
        (
            "PUT",
            prefix(first) + f"/api/scenarios/{scenario_row['id']}",
            {
                "json": {
                    "title": "Forbidden",
                    "graph": scenario_row["graph"],
                    "revision": scenario_row["revision"],
                }
            },
        ),
        ("POST", prefix(first) + "/api/clients/refresh", {}),
        (
            "POST",
            prefix(first) + "/api/broadcasts",
            {"json": {"title": "Forbidden", "message": "x"}},
        ),
        (
            "POST",
            prefix(first) + "/api/media",
            {"files": {"file": ("x.txt", b"x", "text/plain")}},
        ),
    ]
    for method, path, kwargs in requests:
        response = env.client.request(method, path, follow_redirects=False, **kwargs)
        assert response.status_code == 403, (path, response.text)
    assert len(projects.list_projects()) == 3
    assert projects.get_project(first.id).name == first.name
    with session_for(first) as session:
        assert session.query(db.Scenario).one().title == "Unchanged"
        assert session.query(db.Campaign).one().promo_code == "UNCHANGED"
        assert session.query(db.MediaAsset).count() == 0
        assert session.query(db.Broadcast).count() == 0
    assert not env.network.calls


def test_csrf_token_is_session_bound_and_form_submission_works(project_env):
    env = project_env
    token = login(env.client)
    # No lifespan here: this browser shares the already-running test application.
    other = TestClient(main.app)
    try:
        assert login(other) != token
        other.headers["X-CSRF-Token"] = token
        assert (
            other.post("/projects", data={"name": "No", "group_id": "456"}).status_code
            == 403
        )
    finally:
        other.close()
    env.client.headers.pop("X-CSRF-Token")
    response = env.client.post(
        "/projects",
        data={
            "name": "Form CSRF",
            "group_id": "456",
            "csrf_token": token,
            **credentials(456),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert projects.get_project_by_group_id(456) is not None


def test_same_campaign_scenario_and_user_ids_are_isolated_end_to_end(two_projects):
    env, first, second = two_projects
    campaigns = [
        campaign(env.client, p, code)
        for p, code in ((first, "ALPHA"), (second, "BETA"))
    ]
    flows = [
        scenario(env.client, p, title)
        for p, title in ((first, "Alpha question"), (second, "Beta question"))
    ]
    assert campaigns[0] == campaigns[1]  # Colliding local IDs are intentional.
    assert flows[0]["id"] == flows[1]["id"]
    for project, label in ((first, "ALPHA"), (second, "BETA")):
        assert callback(env.client, project, comment(project)).text == "ok"
        assert callback(env.client, project, message(project)).text == "ok"
        # Duplicate deliveries stay idempotent within each group.
        assert callback(env.client, project, comment(project)).text == "ok"
        assert callback(env.client, project, message(project)).text == "ok"
        with session_for(project) as session:
            assert session.query(db.Campaign).one().promo_code == label
            assert session.query(db.ProcessedComment).one().status == "sent"
            assert session.query(db.DialogEvent).count() == 1
            assert session.get(db.Conversation, 77).node_id == "answer"
            assert session.query(db.Scenario).one().active
    sent = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert [(c["project_id"], c["params"]["message"]) for c in sent] == [
        (first.id, "Promo ALPHA"),
        (first.id, "Alpha question"),
        (second.id, "Promo BETA"),
        (second.id, "Beta question"),
    ]
    result = env.client.post(
        prefix(first) + f"/api/scenarios/{flows[0]['id']}/pause",
        json={"revision": flows[0]["revision"]},
    )
    assert result.status_code == 200
    assert (
        env.client.post(prefix(first) + f"/campaigns/{campaigns[0]}/delete").status_code
        == 200
    )
    assert env.client.get(prefix(first) + "/api/scenarios").json()["campaigns"] == []
    other = env.client.get(prefix(second) + "/api/scenarios").json()
    assert other["scenarios"][0]["active"] and len(other["campaigns"]) == 1
    assert env.client.get("/admin/api/clients/77").status_code == 404


@pytest.mark.parametrize(
    "kind", ["message_new", "wall_reply_new", "message_deny", "confirmation"]
)
@pytest.mark.parametrize("invalid", ["secret", "group", "group-string", "group-bool"])
def test_callbacks_reject_wrong_credentials_before_any_write(
    two_projects, kind, invalid
):
    env, first, second = two_projects
    payload = comment(first) if kind == "wall_reply_new" else message(first)
    payload["type"] = kind
    if kind == "message_deny":
        payload["object"] = {"user_id": 77}
    if invalid == "secret":
        payload["secret"] = second.secret
    else:
        payload["group_id"] = {
            "group": second.group_id,
            "group-string": str(first.group_id),
            "group-bool": True,
        }[invalid]
    response = callback(env.client, first, payload)
    if invalid == "secret":
        assert response.text == "invalid secret"
    else:
        assert response.status_code == 403
    for project in (first, second):
        with session_for(project) as session:
            for model in (
                db.Client,
                db.Conversation,
                db.DialogEvent,
                db.ProcessedComment,
                db.PendingGift,
            ):
                assert session.query(model).count() == 0, model.__name__
    assert not env.network.calls


def test_confirmation_and_legacy_callback_never_select_another_project(two_projects):
    env, first, second = two_projects
    legacy = next(p for p in projects.list_projects() if p.is_legacy)
    for project in (legacy, first, second):
        payload = {
            "type": "confirmation",
            "group_id": project.group_id,
            "secret": project.secret,
        }
        assert callback(env.client, project, payload).text == project.confirmation
    paused = configure(env.client, first, enabled=False)
    assert (
        callback(
            env.client,
            paused,
            {
                "type": "confirmation",
                "group_id": paused.group_id,
                "secret": paused.secret,
            },
        ).text
        == paused.confirmation
    )
    assert env.client.post("/vk/callback", json=message(first)).text == "invalid secret"
    wrong_group = message(first)
    wrong_group["secret"] = legacy.secret
    assert env.client.post("/vk/callback", json=wrong_group).status_code == 403
    assert env.client.post("/vk/callback", json=message(legacy)).text == "ok"
    assert env.client.get("/admin/api/clients/77").status_code == 200
    for project in (first, second):
        assert env.client.get(prefix(project) + "/api/clients/77").status_code == 404


def test_media_and_foreign_resource_ids_cannot_cross_project_routes(two_projects):
    env, first, second = two_projects
    first_flow = scenario(env.client, first, "Private scenario")
    response = env.client.post(
        prefix(first) + "/api/media",
        files={
            "file": ("private.txt", b"alpha-private-media", "text/plain"),
        },
    )
    assert response.status_code == 200, response.text
    asset_id = response.json()["id"]
    assert (
        env.client.get(prefix(first) + f"/api/media/{asset_id}").content
        == b"alpha-private-media"
    )
    for base_path in (prefix(second), "/admin"):
        assert env.client.get(base_path + f"/api/media/{asset_id}").status_code == 404
        assert env.client.get(base_path + "/api/scenarios").json()["media"] == []
        response = env.client.put(
            base_path + f"/api/scenarios/{first_flow['id']}",
            json={
                "title": "Injected",
                "graph": first_flow["graph"],
                "revision": first_flow["revision"],
            },
        )
        assert response.status_code == 404
        assert (
            env.client.post(
                base_path + f"/api/scenarios/{first_flow['id']}/publish",
                json={"revision": first_flow["revision"]},
            ).status_code
            == 404
        )
    other_upload = env.client.post(
        prefix(second) + "/api/media",
        files={
            "file": ("private.txt", b"beta-private-media", "text/plain"),
        },
    )
    assert other_upload.status_code == 200
    other_id = other_upload.json()["id"]
    assert other_id != asset_id
    assert (
        env.client.get(prefix(second) + f"/api/media/{other_id}").content
        == b"beta-private-media"
    )
    assert env.client.get(prefix(first) + f"/api/media/{other_id}").status_code == 404
    assert (
        env.client.get(prefix(first) + f"/api/media/{asset_id}").content
        == b"alpha-private-media"
    )
    graph = copy.deepcopy(first_flow["graph"])
    graph["nodes"][1]["media_id"] = asset_id
    assert (
        env.client.post(prefix(first) + "/api/scenarios/validate", json=graph).json()[
            "errors"
        ]
        == []
    )
    assert env.client.post(
        prefix(second) + "/api/scenarios/validate", json=graph
    ).json()["errors"]
    with session_for(first) as session:
        asset = session.get(db.MediaAsset, asset_id)
        assert Path(asset.path).resolve().is_relative_to(env.root.resolve())
        assert Path(asset.path).read_bytes() == b"alpha-private-media"


def test_client_mutations_conversation_resume_and_bot_settings_are_local(two_projects):
    env, first, second = two_projects
    for project in (first, second):
        assert callback(env.client, project, message(project)).text == "ok"
        with session_for(project) as session:
            session.get(db.Conversation, 77).handoff = True
            session.commit()
    assert (
        env.client.post(prefix(first) + "/api/clients/77/unsubscribe").status_code
        == 200
    )
    assert (
        env.client.post(prefix(first) + "/api/conversations/77/resume").status_code
        == 200
    )
    response = env.client.post(
        prefix(first) + "/chat-settings",
        data={
            "chat_enabled": "1",
            "chat_greeting": "Only Alpha greeting",
            "operator_user_id": "101",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(prefix(first) + "?")
    response = env.client.post(
        prefix(first) + "/settings", data={"test_mode": "1"}, follow_redirects=False
    )
    assert response.status_code == 303
    first_detail = env.client.get(prefix(first) + "/api/clients/77").json()
    second_detail = env.client.get(prefix(second) + "/api/clients/77").json()
    assert first_detail["client"]["unsubscribed"] and not first_detail["handoff"]
    assert not second_detail["client"]["unsubscribed"] and second_detail["handoff"]
    with session_for(first) as session:
        values = db.read_settings(session)
        assert values["chat_greeting"] == "Only Alpha greeting"
        assert values["operator_user_id"] == "101" and values["test_mode"] == "true"
    with session_for(second) as session:
        values = db.read_settings(session)
        assert values.get("chat_greeting") != "Only Alpha greeting"
        assert (
            values.get("operator_user_id") != "101"
            and values.get("test_mode") != "true"
        )


def test_callback_valid_envelope_cannot_import_another_groups_comment(two_projects):
    env, first, second = two_projects
    payload = comment(first)
    payload["object"]["owner_id"] = -second.group_id
    assert callback(env.client, first, payload).text == "ok"
    for project in (first, second):
        with session_for(project) as session:
            assert session.query(db.ProcessedComment).count() == 0
            assert session.query(db.Client).count() == 0
    assert not env.network.calls


def test_project_pages_mask_secrets_and_do_not_leak_csrf_to_vk(two_projects):
    env, first, second = two_projects
    paused = configure(env.client, second, enabled=False)
    secrets_to_hide = [env.base.admin_password, env.base.admin_session_secret]
    for project in projects.list_projects():
        secrets_to_hide.extend((project.token, project.secret, project.confirmation))
    paths = ["/projects"] + [
        prefix(p) + f"?section={section}"
        for p in (first, paused)
        for section in (
            "scenarios",
            "campaigns",
            "clients",
            "broadcasts",
            "chat",
            "stats",
            "settings",
        )
    ]
    for path in paths:
        response = env.client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "no-store" in response.headers["cache-control"]
        for value in secrets_to_hide:
            assert value not in response.text
        assert env.client.headers["X-CSRF-Token"] in response.text
        if path != "/projects":
            assert f'data-project-prefix="{path.split("?")[0][:-6]}"' in response.text
            assert 'href="/admin' not in response.text
            assert 'action="/admin' not in response.text
    overview = env.client.get("/projects").text
    assert "На паузе" in overview and "Включён" in overview
    for project in (first, second):
        assert f"/vk/callback/{project.id}" in overview
        assert env.client.post(f"/projects/{project.id}/check").status_code == 200
    csrf = env.client.headers["X-CSRF-Token"]
    for call in env.network.calls:
        assert "x-csrf-token" not in call["headers"]
        assert csrf not in json.dumps(call)
    cookie = env.client.cookies.get("session").split(".", 1)[0]
    session_data = base64.b64decode(cookie + "=" * (-len(cookie) % 4)).decode()
    assert all(value not in session_data for value in secrets_to_hide)
    assert csrf not in env.client.get("/health").text


def test_connection_check_uses_token_owner_and_sanitizes_vk_errors(two_projects):
    env, first, second = two_projects
    response = env.client.post(f"/projects/{first.id}/check")
    assert response.status_code == 200
    (call,) = env.network.calls
    assert call["method"] == "groups.getById" and "group_id" not in call["params"]
    assert call["params"]["access_token"] == first.token
    env.network.replies["groups.getById"] = {
        "response": {"groups": [{"id": second.group_id}]}
    }
    assert env.client.post(f"/projects/{first.id}/check").status_code == 422
    env.network.replies["groups.getById"] = {
        "error": {
            "error_code": 5,
            "error_msg": f"Echoed secret {first.token} {first.secret}",
        }
    }
    response = env.client.post(f"/projects/{first.id}/check")
    assert response.status_code == 422
    assert first.token not in response.text and first.secret not in response.text
    assert all(call["method"] == "groups.getById" for call in env.network.calls)


def test_concurrent_scopes_propagate_to_anyio_threads_and_reset(two_projects):
    env, first, second = two_projects
    barrier = threading.Barrier(2, timeout=10)

    def thread_snapshot():
        barrier.wait()
        settings = config.get_settings()
        with db.SessionLocal() as session:
            return settings.vk_group_id, settings.vk_group_token, str(session.bind.url)

    async def scoped(project):
        with projects.project_scope(project):
            before = config.get_settings()
            result = await anyio.to_thread.run_sync(thread_snapshot)
            assert result[:2] == (project.group_id, project.token)
            assert result[2] == str(db.get_project_engine(project).url)
            assert await vk_api.is_group_member(77)
            assert config.get_settings().vk_group_token == before.vk_group_token
            with pytest.raises(RuntimeError, match="scope cleanup"):
                with projects.project_scope(None):
                    assert config.get_settings().vk_group_id == 123
                    raise RuntimeError("scope cleanup")
            assert projects.current_project.get().id == project.id

    async def run():
        await asyncio.gather(scoped(first), scoped(second))
        assert projects.current_project.get() is None

    asyncio.run(run())
    assert projects.current_project.get() is None
    assert config.get_settings().vk_group_token == env.base.vk_group_token
    assert {
        (c["params"]["access_token"], c["params"]["group_id"])
        for c in env.network.calls
    } == {
        (first.token, str(first.group_id)),
        (second.token, str(second.group_id)),
    }


def test_concurrent_http_callbacks_keep_same_user_in_separate_databases(two_projects):
    env, first, second = two_projects
    scenario(env.client, first, "Concurrent Alpha")
    scenario(env.client, second, "Concurrent Beta")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(f"/vk/callback/{p.id}", json=message(p))
                    for p in (first, second)
                ]
            )
        assert all(r.status_code == 200 and r.text == "ok" for r in responses)

    asyncio.run(run())
    sent = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert {(c["project_id"], c["params"]["message"]) for c in sent} == {
        (first.id, "Concurrent Alpha"),
        (second.id, "Concurrent Beta"),
    }
    for project in (first, second):
        with session_for(project) as session:
            assert session.get(db.Conversation, 77).node_id == "answer"
            assert session.query(db.DialogEvent).one().status == "done"
            assert session.query(db.BotMessage).one().user_id == 77
            assert session.get(db.Client, 77).bot_contacted_at is not None
    assert projects.current_project.get() is None


def test_callback_greeting_persists_outgoing_audit_and_contact_in_its_project(
    two_projects,
):
    env, first, second = two_projects
    for _ in range(2):
        assert callback(env.client, first, message(first)).text == "ok"
    (sent,) = [call for call in env.network.calls if call["method"] == "messages.send"]
    assert sent["project_id"] == first.id
    with session_for(first) as session:
        client = session.get(db.Client, 77)
        assert client.bot_contacted_at is not None and client.messages_allowed
        audit = session.query(db.BotMessage).one()
        assert audit.user_id == 77
        assert audit.event_key == f"send:77:{sent['params']['random_id']}"
        assert session.query(db.DialogEvent).one().status == "done"
    legacy = next(project for project in projects.list_projects() if project.is_legacy)
    for project in (second, legacy):
        with session_for(project) as session:
            assert session.get(db.Client, 77) is None
            assert session.query(db.BotMessage).count() == 0
    assert env.client.get(prefix(first) + "/api/broadcasts").json()["eligible"] == 1
    assert env.client.get(prefix(second) + "/api/broadcasts").json()["eligible"] == 0


def test_queued_jobs_optouts_and_worker_leases_stay_in_their_project(two_projects):
    env, first, second = two_projects
    for project in (first, second):
        assert callback(env.client, project, message(project)).text == "ok"
        with session_for(project) as session:
            assert session.get(db.Client, 77).bot_contacted_at is not None
            assert session.query(db.BotMessage).count() == 1
    first_job = queued_broadcast(env.client, first, "Alpha mailing")
    second_job = queued_broadcast(env.client, second, "Beta mailing")
    assert callback(env.client, first, message(first, "stop", number=2)).text == "ok"
    assert env.client.get(prefix(first) + "/api/clients/77").json()["client"][
        "unsubscribed"
    ]
    assert not env.client.get(prefix(second) + "/api/clients/77").json()["client"][
        "unsubscribed"
    ]
    for project, foreign_job in ((first, second_job), (second, first_job)):
        path = prefix(project) + f"/api/broadcasts/{foreign_job['id']}"
        assert env.client.get(path).status_code == 404
        assert env.client.post(path + "/cancel").status_code == 404
    env.network.calls.clear()

    async def tick(project):
        with projects.project_scope(project):
            await broadcasts.worker_tick()

    async def run():
        await asyncio.gather(tick(first), tick(second))

    asyncio.run(run())
    first_detail = env.client.get(
        prefix(first) + f"/api/broadcasts/{first_job['id']}"
    ).json()
    second_detail = env.client.get(
        prefix(second) + f"/api/broadcasts/{second_job['id']}"
    ).json()
    assert first_detail["recipients"][0]["status"] == "skipped"
    assert second_detail["recipients"][0]["status"] == "sent"
    (sent,) = [c for c in env.network.calls if c["method"] == "messages.send"]
    assert sent["project_id"] == second.id and sent["params"]["message"].startswith(
        "Beta mailing"
    )
    for project in (first, second):
        with session_for(project) as session:
            assert session.query(db.WorkLease).one().until is None


def test_pausing_blocks_callbacks_test_sends_and_stale_worker_context(two_projects):
    env, first, second = two_projects
    assert callback(env.client, first, message(first)).text == "ok"
    job = queued_broadcast(env.client, first, "Must not send")
    campaign_id = campaign(env.client, first, "PAUSED")
    paused = configure(env.client, first, enabled=False)
    env.network.calls.clear()
    assert callback(env.client, paused, message(paused, number=2, user=88)).text == "ok"
    assert callback(env.client, paused, comment(paused)).text == "ok"
    response = env.client.post(
        prefix(paused) + "/test-send",
        data={
            "campaign_id": str(campaign_id),
            "user_id": "77",
        },
        follow_redirects=False,
    )
    assert response.status_code in (303, 422)
    response = env.client.post(
        prefix(paused) + f"/api/broadcasts/{job['id']}/test", json={"user_id": 77}
    )
    assert response.status_code == 422

    async def stale_worker():
        # Existing tasks can still hold the immutable pre-pause Project object.
        with projects.project_scope(first):
            with pytest.raises(vk_api.VkApiError):
                await vk_api.send_message(77, "stale context", random_id=12345)
            await broadcasts.worker_tick()

    asyncio.run(stale_worker())
    assert not env.network.calls
    with session_for(paused) as session:
        assert session.get(db.Client, 88) is None
        assert session.query(db.ProcessedComment).count() == 0
        recipient = session.query(db.BroadcastRecipient).one()
        assert recipient.status == "pending" and recipient.attempts == 0
        assert session.query(db.Broadcast).one().status == "queued"
        assert session.query(db.WorkLease).count() == 0
    assert callback(env.client, second, message(second)).text == "ok"
    assert {call["project_id"] for call in env.network.calls} == {second.id}


@pytest.mark.parametrize("optout", ["stop", "button", "deny"])
def test_paused_optout_is_local_silent_and_excludes_queued_send_after_resume(
    two_projects, optout
):
    env, first, second = two_projects
    for project in (first, second):
        assert callback(env.client, project, message(project)).text == "ok"
    first_job = queued_broadcast(env.client, first, "Paused Alpha queue")
    second_job = queued_broadcast(env.client, second, "Active Beta queue")
    paused = configure(env.client, first, enabled=False)
    env.network.calls.clear()
    payload = message(paused, "Стоп", number=2)
    if optout == "button":
        payload["object"]["message"].update(
            text="Отписаться от рассылок",
            payload=json.dumps({"action": "broadcast_unsubscribe"}),
        )
    elif optout == "deny":
        payload.update(type="message_deny", object={"user_id": 77})
    for _ in range(2):
        assert callback(env.client, paused, payload).text == "ok"
    first_client = env.client.get(prefix(paused) + "/api/clients/77").json()["client"]
    second_client = env.client.get(prefix(second) + "/api/clients/77").json()["client"]
    assert first_client["unsubscribed"] and not second_client["unsubscribed"]
    if optout == "deny":
        assert first_client["messages_allowed"] is False
    assert not env.network.calls

    # VK message permission is separate from newsletter consent. Neither it nor
    # a subscribe message is allowed to reactivate a paused project's delivery.
    assert (
        callback(
            env.client,
            paused,
            {
                "type": "message_allow",
                "group_id": paused.group_id,
                "secret": paused.secret,
                "object": {"user_id": 77},
            },
        ).text
        == "ok"
    )
    assert (
        callback(env.client, paused, message(paused, "/subscribe", number=3)).text
        == "ok"
    )
    assert (
        callback(env.client, paused, message(paused, "hello", number=4, user=88)).text
        == "ok"
    )
    assert not projects.get_project(paused.id).enabled
    with session_for(paused) as session:
        client = session.get(db.Client, 77)
        assert client.unsubscribed and client.messages_allowed
        assert session.get(db.Client, 88) is None
        assert session.query(db.Conversation).count() == 1
        assert session.query(db.DialogEvent).count() == (1 if optout == "deny" else 2)
        assert session.query(db.BroadcastRecipient).one().status == "pending"
    assert not env.network.calls

    resumed = configure(env.client, paused)

    async def tick(project):
        with projects.project_scope(project):
            await broadcasts.worker_tick()

    async def run():
        await asyncio.gather(tick(resumed), tick(second))

    asyncio.run(run())
    first_detail = env.client.get(
        prefix(resumed) + f"/api/broadcasts/{first_job['id']}"
    ).json()
    second_detail = env.client.get(
        prefix(second) + f"/api/broadcasts/{second_job['id']}"
    ).json()
    assert first_detail["recipients"][0]["status"] == "skipped"
    assert second_detail["recipients"][0]["status"] == "sent"
    assert {call["project_id"] for call in env.network.calls} == {second.id}


@pytest.mark.parametrize(
    "project_env", [{"vk_group_id": 0, "vk_group_token": ""}], indirect=True
)
def test_fresh_install_does_not_reassign_legacy_routes_to_first_new_project(
    project_env,
):
    env = project_env
    assert projects.list_projects() == []
    login(env.client)
    project = configure(env.client, create(env.client))
    assert not project.is_legacy
    assert env.client.post("/vk/callback", json=message(project)).status_code == 404
    assert env.client.get("/admin/api/scenarios").status_code == 404
    response = env.client.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/projects"
    assert callback(env.client, project, message(project)).text == "ok"
    with db.SessionLocal() as session:
        assert session.get(db.Client, 77) is None


def test_unknown_project_paths_never_fall_back_to_legacy(two_projects):
    env, first, _ = two_projects
    absent = max(p.id for p in projects.list_projects()) + 1000
    for ident in (str(absent), "0", "-1", "not-a-project"):
        assert env.client.get(f"/p/{ident}/admin/api/clients").status_code == 404
        assert (
            env.client.post(f"/vk/callback/{ident}", json=message(first)).status_code
            == 404
        )
    assert not env.network.calls


def test_trailing_slash_redirects_keep_the_selected_project(two_projects):
    env, first, second = two_projects
    scenario(env.client, first, "Alpha redirect sentinel")
    scenario(env.client, second, "Beta redirect sentinel")
    for suffix in ("/", "/api/scenarios/"):
        path = prefix(second) + suffix
        redirect = env.client.get(path, follow_redirects=False)
        assert redirect.status_code == 307
        assert urlsplit(redirect.headers["location"]).path == path.rstrip("/")
        response = env.client.get(path)
        assert response.status_code == 200, response.text
        assert response.history and response.url.path == path.rstrip("/")
        assert all(
            item.url.path.startswith(prefix(second)) for item in response.history
        )
        if suffix == "/":
            assert f'data-project-id="{second.id}"' in response.text
            assert f'data-project-prefix="/p/{second.id}"' in response.text
        else:
            assert [row["title"] for row in response.json()["scenarios"]] == [
                "Beta redirect sentinel"
            ]
    assert not env.network.calls
