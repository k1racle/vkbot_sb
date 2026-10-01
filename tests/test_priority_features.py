import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from test_scenarios import setup as setup, login, event
from test_flow_modules import graph, install
from app import db, diagnostics, vk_api
from test_waits import timer as timer, tick, wait_graph
from datetime import timedelta


def contact_flow(**options):
    return graph(
        {
            "id": "contact",
            "type": "contact",
            "text": "Оставьте контакт",
            "variable": "contact",
            "contact_types": ["phone", "email", "messenger"],
            **options,
        }
    )


def test_contacts_journey_and_notifications(setup):
    client, sessions, sent = setup
    flow = install(client, contact_flow())
    with sessions() as session:
        db.save_settings(
            session,
            {
                "notify_contacts": "true",
                "notify_completed": "true",
                "operator_user_id": "99",
            },
        )
    client.post("/vk/callback", json=event())
    answer = event("+79991234567, anna@example.com; https://t.me/anna", 2)
    assert client.post("/vk/callback", json=answer).status_code == 200
    count = sent.call_count
    assert client.post("/vk/callback", json=answer).status_code == 200
    assert sent.call_count == count
    notices = [c for c in sent.call_args_list if c.args[0] == 99]
    assert len(notices) == 2
    assert "anna@example.com" in notices[0].args[1]
    assert "gim123?sel=77" in notices[0].args[1]
    assert notices[0].kwargs["random_id"] != notices[1].kwargs["random_id"]
    detail = client.get("/admin/api/clients/77").json()
    assert detail["client"]["email"] == "anna@example.com"
    assert detail["client"]["messenger"] == "https://t.me/anna"
    assert detail["client"]["contact_details"]["email"]["scenario_id"] == flow["id"]
    assert detail["client"]["contact_details"]["email"]["date"].endswith("Z")
    assert detail["events"][0]["journey"][-1]["phase"] == "completed"
    assert detail["events"][1]["journey"][-1]["phase"] == "waiting"
    assert client.get("/admin/api/clients?q=anna@example.com").json()["total"] == 1


def test_notifications_off_by_default_and_preview_has_no_sends(setup):
    client, _, sent = setup
    g = contact_flow(notify_manager=True)
    install(client, g)
    sent.reset_mock()
    response = client.post(
        "/admin/api/preview", json={"graph": g, "state": {}, "text": "start"}
    )
    assert response.status_code == 200
    sent.assert_not_called()


def test_notice_failure_does_not_rollback_contact(setup, monkeypatch):
    client, sessions, _ = setup
    install(client, contact_flow())
    with sessions() as session:
        db.save_settings(session, {"notify_contacts": "true", "operator_user_id": "99"})

    async def send(user, message, **kwargs):
        if user == 99:
            raise vk_api.VkApiError("901: forbidden secret-must-not-be-stored")

    monkeypatch.setattr(vk_api, "send_message", send)
    client.post("/vk/callback", json=event())
    client.post("/vk/callback", json=event("anna@example.com", 2))
    with sessions() as session:
        assert session.get(db.Client, 77).email == "anna@example.com"
        row = session.query(db.DialogEvent).order_by(db.DialogEvent.id.desc()).first()
        assert row.status == "done" and "901" in row.error
        assert "secret-must" not in row.error


def test_stage_notification_and_error_trace(setup, monkeypatch):
    client, sessions, sent = setup
    install(client, contact_flow(notify_manager=True))
    with sessions() as session:
        db.save_settings(session, {"operator_user_id": "99"})
    client.post("/vk/callback", json=event())
    assert len([c for c in sent.call_args_list if c.args[0] == 99]) == 1
    with sessions() as session:
        row = session.query(db.DialogEvent).one()
        assert [s["node_id"] for s in row.journey if s["phase"] == "entered"] == [
            "start",
            "contact",
        ]
    monkeypatch.setattr(
        vk_api, "send_message", AsyncMock(side_effect=vk_api.VkApiError("6: temporary"))
    )
    with pytest.raises(vk_api.VkApiError):
        client.post("/vk/callback", json=event("меню", 2))
    with sessions() as session:
        row = session.query(db.DialogEvent).order_by(db.DialogEvent.id.desc()).first()
        assert row.status == "failed"
        assert row.journey[-1]["phase"] == "error"


def test_authenticated_callback_receipt_includes_ignored_comments(setup):
    client, sessions, _ = setup
    payload = {
        "group_id": 123,
        "secret": "wrong",
        "type": "video_comment_new",
        "object": {},
    }
    client.post("/vk/callback", json=payload)
    with sessions() as session:
        assert "last_vk_callback" not in db.read_settings(session)
    payload["secret"] = "test-secret"
    client.post("/vk/callback", json=payload)
    with sessions() as session:
        receipt = json.loads(db.read_settings(session)["last_vk_callback"])
        assert receipt["type"] == "video_comment_new"
        assert "пропущен" in receipt["reason"]
        assert "secret" not in str(receipt)


def test_diagnostics_checks_only_read_methods_and_redacts(setup, monkeypatch):
    _, _, sent = setup
    project = SimpleNamespace(
        id=1,
        group_id=123,
        token="private-group",
        video_token="private-user",
        callback_secret="private-secret",
        enabled=True,
        is_legacy=True,
    )

    async def query(method, token, **params):
        if method == "groups.getById":
            return {"groups": [{"id": 123, "is_admin": 1}]}
        if method == "groups.getTokenPermissions":
            return {"permissions": [{"name": "messages", "setting": 1}]}
        if method == "account.getAppPermissions":
            return 16
        if method == "groups.getCallbackServers":
            return {
                "items": [
                    {
                        "id": 5,
                        "url": "https://bot.test/vk/callback/1",
                        "status": "ok",
                        "secret_key": "private-secret",
                    }
                ]
            }
        if method == "groups.getCallbackSettings":
            return {"events": {k: 1 for k in diagnostics.EVENTS}}
        raise AssertionError(method)

    monkeypatch.setattr(vk_api, "_request", query)
    report = asyncio.run(diagnostics.connection_report(project, "https://bot.test"))
    assert all(c["status"] == "ok" for c in report["checks"])
    assert "private-" not in json.dumps(report)
    sent.assert_not_called()
    monkeypatch.setattr(
        vk_api,
        "_request",
        AsyncMock(side_effect=vk_api.VkApiError("5: private-group private-user")),
    )
    report = asyncio.run(diagnostics.connection_report(project, "https://bot.test"))
    assert "private-" not in json.dumps(report)
    assert any(c["status"] == "error" for c in report["checks"])


def test_diagnostics_auth_and_csrf(setup, monkeypatch):
    client, _, _ = setup
    request = AsyncMock()
    monkeypatch.setattr(vk_api, "_request", request)
    assert client.post("/admin/api/vk-diagnostics").status_code == 401
    login(client)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/admin/api/vk-diagnostics").status_code == 403
    request.assert_not_called()


def test_timer_completion_records_journey_and_notifies_once(timer):
    client, sessions, sent, clock, _ = timer
    install(client, wait_graph(1, "seconds"))
    with sessions() as session:
        db.save_settings(
            session, {"operator_user_id": "99", "notify_completed": "true"}
        )
    client.post("/vk/callback", json=event())
    clock[0] += timedelta(seconds=2)
    tick()
    tick()
    with sessions() as session:
        row = session.query(db.DialogEvent).filter_by(kind="wait").one()
        assert row.journey[-1]["phase"] == "completed"
        assert row.journey[0]["detail"] == "Таймер"
    assert len([c for c in sent.call_args_list if c.args[0] == 99]) == 1


def test_additive_migration_preserves_existing_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db.init_db(engine)
    with Session(engine) as session:
        session.add(db.Client(user_id=77, first_name="Анна"))
        session.commit()
    with engine.begin() as conn:
        for column in ("email", "messenger", "contact_details"):
            conn.execute(text(f"ALTER TABLE clients DROP COLUMN {column}"))
        conn.execute(text("ALTER TABLE dialog_events DROP COLUMN journey"))
    db.init_db(engine)
    db.init_db(engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT first_name, email, messenger, contact_details FROM clients WHERE user_id=77"
            )
        ).one()
        assert tuple(row) == ("Анна", "", "", "{}")
    engine.dispose()
