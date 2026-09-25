import asyncio
import copy
import json
import os
from unittest.mock import AsyncMock

# Tests must never load a user's database or VK credentials.
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
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, dialog, main, scenario_api, vk_api
from app.flows import Graph, starter_graph, validate_graph


@pytest.fixture
def setup(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    for module in (main, db, dialog, scenario_api):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(main, "ATTACHMENT_DIR", tmp_path)
    db.init_db()
    sent = AsyncMock()
    monkeypatch.setattr(vk_api, "send_message", sent)
    monkeypatch.setattr(vk_api, "get_user_name", AsyncMock(return_value="Анна"))
    monkeypatch.setattr(vk_api, "is_group_member", AsyncMock(return_value=True))
    with TestClient(main.app) as client:
        yield client, sessions, sent
    engine.dispose()


def login(client):
    result = client.post(
        "/login", data={"username": "admin", "password": "test-password"}
    )
    assert result.status_code == 200
    import re

    csrf = re.search(r'name="csrf-token" content="([^"]+)"', result.text)[1]
    client.headers["X-CSRF-Token"] = csrf


def create(client):
    login(client)
    response = client.post("/admin/api/scenarios")
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, flow):
    response = client.post(
        f"/admin/api/scenarios/{flow['id']}/publish",
        json={"revision": flow["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def event(text="привет", number=1, user=77, payload=None):
    message = {
        "from_id": user,
        "peer_id": user,
        "conversation_message_id": number,
        "text": text,
    }
    if payload:
        message["payload"] = payload
    return {
        "type": "message_new",
        "group_id": 123,
        "event_id": f"{user}:{number}",
        "secret": "test-secret",
        "object": {"message": message, "client_info": {"keyboard": True}},
    }


def test_auth_and_csrf(setup):
    client, _, _ = setup
    assert client.get("/admin/api/scenarios").status_code == 401
    login(client)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/admin/api/scenarios").status_code == 403


def test_pages_render(setup):
    client, _, _ = setup
    login(client)
    for section in ("scenarios", "campaigns", "chat", "settings", "clients", "stats"):
        response = client.get(f"/admin?section={section}")
        assert response.status_code == 200, response.text
        assert "VK Бот" in response.text
    assert client.get("/static/flows.js").status_code == 200


def test_draft_publish_isolation_and_revision_conflict(setup):
    client, sessions, _ = setup
    flow = publish(client, create(client))
    graph = copy.deepcopy(flow["graph"])
    graph["nodes"][1]["text"] = "Новый черновик"
    result = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={"title": "Правки", "graph": graph, "revision": flow["revision"]},
    )
    assert result.status_code == 200
    with sessions() as session:
        row = session.get(db.Scenario, flow["id"])
        assert row.published["nodes"][1]["text"] != row.draft["nodes"][1]["text"]
    assert (
        client.put(
            f"/admin/api/scenarios/{flow['id']}",
            json={
                "title": "Старая вкладка",
                "graph": graph,
                "revision": flow["revision"],
            },
        ).status_code
        == 409
    )


def test_only_one_active_scenario(setup):
    client, sessions, _ = setup
    first = publish(client, create(client))
    second = publish(client, client.post("/admin/api/scenarios").json())
    with sessions() as session:
        assert not session.get(db.Scenario, first["id"]).active
        assert session.get(db.Scenario, second["id"]).active


def test_invalid_graph_is_saved_but_not_published(setup):
    client, _, _ = setup
    flow = create(client)
    graph = flow["graph"]
    graph["nodes"][0]["next"] = "missing"
    flow = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={"title": flow["title"], "graph": graph, "revision": 0},
    ).json()
    assert (
        client.post(
            f"/admin/api/scenarios/{flow['id']}/publish",
            json={"revision": flow["revision"]},
        ).status_code
        == 422
    )


def test_graph_validation_cycles_links_and_variables():
    graph = starter_graph()
    assert validate_graph(graph) == []
    graph["nodes"][0]["next"] = "start"
    assert any("цикл" in e for e in validate_graph(graph))
    graph = starter_graph()
    graph["nodes"][1]["buttons"][1]["url"] = "javascript:alert(1)"
    graph["nodes"][2]["variable"] = "first_name"
    assert len(validate_graph(graph)) == 2


def test_preview_uses_interpreter_without_vk(setup):
    client, _, sent = setup
    flow = create(client)
    result = client.post(
        "/admin/api/preview", json={"graph": flow["graph"], "restart": True}
    ).json()
    assert "Анна" in result["messages"][0]["text"]
    payload = json.loads(
        result["messages"][0]["keyboard"]["buttons"][0][0]["action"]["payload"]
    )
    result = client.post(
        "/admin/api/preview",
        json={"graph": flow["graph"], "state": result["state"], "payload": payload},
    ).json()
    assert result["messages"][0]["text"] == "Расскажите, что ищете?"
    assert result["state"]["node_id"] == "question"
    result = client.post(
        "/admin/api/preview",
        json={"graph": flow["graph"], "state": result["state"], "text": "платье"},
    ).json()
    assert result["state"]["handoff"] is True
    assert "платье" in result["messages"][0]["text"]
    sent.assert_not_called()


def test_vk_nested_messages_dedup_and_state_survives_new_session(setup):
    client, sessions, sent = setup
    publish(client, create(client))
    assert client.post("/vk/callback", json=event()).text == "ok"
    keyboard = sent.call_args.kwargs["keyboard"]
    button = keyboard["buttons"][0][0]["action"]["payload"]
    assert client.post("/vk/callback", json=event()).text == "ok"
    assert sent.call_count == 1
    client.post("/vk/callback", json=event("Подобрать товар", 2, payload=button))
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "question"
    client.post("/vk/callback", json=event("платье", 3))
    with sessions() as session:
        state = session.get(db.Conversation, 77)
        assert state.handoff
        assert state.variables["request"] == "платье"
    count = sent.call_count
    client.post("/vk/callback", json=event("я жду", 4))
    assert sent.call_count == count
    client.post("/vk/callback", json=event("меню", 5))
    assert sent.call_count == count + 1
    with sessions() as session:
        assert not session.get(db.Conversation, 77).handoff


def test_two_users_and_old_keyboard_are_isolated(setup):
    client, sessions, sent = setup
    publish(client, create(client))
    client.post("/vk/callback", json=event())
    button = sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"]
    client.post("/vk/callback", json=event(user=88))
    client.post("/vk/callback", json=event(number=2, user=88, payload=button))
    assert "устарела" in sent.call_args.args[1]
    with sessions() as session:
        assert session.get(db.Conversation, 77).node_id == "welcome"
        assert session.get(db.Conversation, 88).node_id == "welcome"


def test_republish_invalidates_old_buttons(setup):
    client, _, sent = setup
    flow = publish(client, create(client))
    client.post("/vk/callback", json=event())
    payload = sent.call_args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"]
    publish(client, flow)
    client.post("/vk/callback", json=event(number=2, payload=payload))
    assert "устарела" in sent.call_args.args[1]


def test_resume_is_admin_only(setup):
    client, sessions, _ = setup
    with sessions() as session:
        session.add(db.Conversation(user_id=77, handoff=True))
        session.commit()
    assert client.post("/admin/api/conversations/77/resume").status_code == 401
    login(client)
    assert client.post("/admin/api/conversations/77/resume").status_code == 200
    with sessions() as session:
        assert not session.get(db.Conversation, 77).handoff


def test_conditions_and_promo_idempotency(setup):
    client, sessions, sent = setup
    with sessions() as session:
        campaign = db.Campaign(
            post_id=123,
            title="Акция",
            promo_message="Код {promo_code}",
            promo_code="SALE",
            one_promo_per_user=True,
        )
        session.add(campaign)
        session.commit()
        cid = campaign.id
    flow = create(client)
    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "s", "type": "start", "next": "c"},
                {
                    "id": "c",
                    "type": "condition",
                    "condition": "member",
                    "yes": "p",
                    "no": "n",
                },
                {"id": "p", "type": "promo", "campaign_id": cid, "next": "end"},
                {"id": "n", "type": "message", "text": "Подпишитесь", "next": "end"},
                {"id": "end", "type": "end"},
            ]
        }
    ).model_dump()
    flow = client.put(
        f"/admin/api/scenarios/{flow['id']}",
        json={"graph": graph, "title": "Промо", "revision": 0},
    ).json()
    publish(client, flow)
    client.post("/vk/callback", json=event())
    assert sent.call_args.args[1] == "Код SALE"
    client.post("/vk/callback", json=event("меню", 2))
    assert "уже получали" in sent.call_args.args[1]
    with sessions() as session:
        assert session.query(db.PromoDelivery).count() == 1


def test_keyword_branch(setup):
    client, _, _ = setup
    flow = create(client)
    graph = flow["graph"]
    graph["nodes"][2]["rules"] = [{"words": "доставка, привезти", "target": "manager"}]
    start = client.post(
        "/admin/api/preview", json={"graph": graph, "restart": True}
    ).json()
    state = start["state"]
    state["node_id"] = "question"
    result = client.post(
        "/admin/api/preview",
        json={"graph": graph, "state": state, "text": "А ДОСТАВКА есть?"},
    ).json()
    assert result["state"]["handoff"]
    assert len(result["messages"]) == 1


def test_migration_preserves_old_campaigns(setup):
    _, sessions, _ = setup
    with sessions() as session:
        session.add(db.Campaign(post_id=44, title="Существующая", promo_code="KEEP"))
        session.commit()
    db.init_db()
    db.init_db()
    with sessions() as session:
        assert session.query(db.Campaign).first().promo_code == "KEEP"


def test_upgrade_from_pre_campaign_filters_schema(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(db, "engine", engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, post_id INTEGER, title VARCHAR(120), promo_code VARCHAR(120), shop_url VARCHAR(500), promo_message TEXT, enabled BOOLEAN, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO campaigns (id, post_id, title, promo_code, shop_url, promo_message, enabled) VALUES (1, 33, 'Old', 'KEEP', '', 'Hi', TRUE)"
            )
        )
    db.init_db()
    with sessionmaker(bind=engine)() as session:
        row = session.get(db.Campaign, 1)
        assert row.promo_code == "KEEP"
        assert row.min_comment_length == 1
        assert row.one_promo_per_user is True
        assert row.attachment_path == ""
    engine.dispose()


def test_uploaded_media_and_general_settings(setup, monkeypatch, tmp_path):
    client, sessions, _ = setup
    create(client)
    # Keep uploads in an isolated working directory, templates are not used here.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    result = client.post(
        "/admin/api/media", files={"file": ("guide.txt", b"guide", "text/plain")}
    )
    assert result.status_code == 200
    assert client.get("/admin/api/media/" + result.json()["id"]).content == b"guide"
    assert (
        client.post(
            "/admin/api/media",
            files={"file": ("code.exe", b"test", "application/x-msdownload")},
        ).status_code
        == 422
    )
    client.post(
        "/admin/settings",
        data={"test_mode": "1", "test_trigger_phrase": "тест"},
        follow_redirects=False,
    )
    with sessions() as session:
        values = db.read_settings(session)
        assert set(values) == {"test_mode", "test_trigger_phrase"}


def test_send_failure_retries_same_random_id_without_advancing(setup):
    client, sessions, sent = setup
    publish(client, create(client))
    sent.side_effect = vk_api.VkApiError("temporary error")
    with pytest.raises(vk_api.VkApiError):
        client.post("/vk/callback", json=event())
    failed_random_id = sent.call_args.kwargs["random_id"]
    with sessions() as session:
        assert session.query(db.DialogEvent).first().status == "failed"
        assert session.get(db.Conversation, 77) is None
    sent.side_effect = None
    client.post("/vk/callback", json=event())
    assert sent.call_args.kwargs["random_id"] == failed_random_id
    with sessions() as session:
        assert session.query(db.DialogEvent).first().status == "done"
        assert session.get(db.Conversation, 77).node_id == "welcome"


def test_operator_forbids_dm_still_pauses_bot(setup):
    client, sessions, sent = setup
    publish(client, create(client))
    with sessions() as session:
        db.save_settings(session, {"operator_user_id": "99"})

    async def send(user, *args, **kwargs):
        if user == 99:
            raise vk_api.VkApiError("Cannot send messages")

    sent.side_effect = send
    client.post("/vk/callback", json=event("позови менеджера"))
    with sessions() as session:
        assert session.get(db.Conversation, 77).handoff
        assert "не доставлено" in session.query(db.DialogEvent).first().error
    count = sent.call_count
    client.post("/vk/callback", json=event("жду", 2))
    assert sent.call_count == count


def test_campaign_edit_keeps_id_and_attachment(setup):
    client, sessions, _ = setup
    login(client)
    fields = {
        "title": "Акция",
        "post_id": "90",
        "promo_code": "CODE",
        "shop_url": "https://example.org",
        "promo_message": "Код",
        "enabled": "1",
        "min_comment_length": "3",
    }
    response = client.post(
        "/admin/campaigns",
        data=fields,
        files={"attachment": ("hello.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200
    with sessions() as session:
        campaign = session.query(db.Campaign).first()
        cid, path = campaign.id, campaign.attachment_path
    fields.update(campaign_id=str(cid), post_id="91")
    client.post("/admin/campaigns", data=fields)
    with sessions() as session:
        assert session.query(db.Campaign).count() == 1
        assert session.get(db.Campaign, cid).post_id == 91
        assert session.get(db.Campaign, cid).attachment_path == path


def test_comment_promo_is_scoped_to_campaign_and_shared_with_dialog(setup, monkeypatch):
    client, sessions, _ = setup
    send = AsyncMock()
    monkeypatch.setattr(main, "send_message", send)
    monkeypatch.setattr(main, "get_user_name", AsyncMock(return_value="Анна"))
    monkeypatch.setattr(main, "is_group_member", AsyncMock(return_value=True))
    with sessions() as session:
        first = db.Campaign(
            post_id=1, title="Один", promo_code="ONE", promo_message="{promo_code}"
        )
        second = db.Campaign(
            post_id=2, title="Два", promo_code="TWO", promo_message="{promo_code}"
        )
        session.add_all([first, second])
        session.commit()
        session.add(db.PromoDelivery(user_id=77, campaign_id=first.id))
        session.commit()

    def comment(post, ident):
        return {
            "secret": "test-secret",
            "type": "wall_reply_new",
            "object": {"id": ident, "post_id": post, "from_id": 77, "text": "хорошо"},
        }

    client.post("/vk/callback", json=comment(1, 1))
    send.assert_not_called()
    client.post("/vk/callback", json=comment(2, 2))
    assert send.call_args.args[1] == "TWO"
    client.post("/vk/callback", json=comment(3, 3))
    assert send.call_count == 1
    with sessions() as session:
        assert (
            session.query(db.ProcessedComment).filter_by(comment_id=3).first().status
            == "no_campaign"
        )


def test_parallel_comments_do_not_send_twice(setup, monkeypatch):
    _, sessions, _ = setup
    with sessions() as session:
        session.add(
            db.Campaign(
                post_id=1,
                promo_code="ONCE",
                promo_message="Код",
                one_promo_per_user=True,
            )
        )
        session.commit()
    send = AsyncMock()

    async def slow_member(_):
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(main, "send_message", send)
    monkeypatch.setattr(main, "get_user_name", AsyncMock(return_value="Анна"))
    monkeypatch.setattr(main, "is_group_member", slow_member)

    async def run():
        requests = []
        for number in (1, 2):
            request = AsyncMock()
            request.json.return_value = {
                "type": "wall_reply_new",
                "secret": "test-secret",
                "object": {
                    "id": number,
                    "post_id": 1,
                    "from_id": 77,
                    "text": "подходит",
                },
            }
            requests.append(main.vk_callback(request))
        await asyncio.gather(*requests)

    asyncio.run(run())
    assert send.call_count == 1


def test_video_uses_document_upload_for_community_token(monkeypatch, tmp_path):
    import httpx

    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"test video")
    api = AsyncMock(
        side_effect=[
            {"upload_url": "https://upload.test"},
            {"doc": {"owner_id": -123, "id": 456}},
        ]
    )
    monkeypatch.setattr(vk_api, "call", api)

    class UploadClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            assert "file" in kwargs["files"]
            return httpx.Response(
                200, json={"file": "uploaded"}, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(vk_api.httpx, "AsyncClient", UploadClient)
    result = asyncio.run(
        vk_api.upload_file_for_message(77, sample, "sample.mp4", "video/mp4")
    )
    assert result == "doc-123_456"
    assert api.call_args_list[0].args == ("docs.getMessagesUploadServer",)
    assert api.call_args_list[1].args == ("docs.save",)
