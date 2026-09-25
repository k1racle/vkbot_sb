import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

# Shared fixture establishes dummy credentials BEFORE importing the application.
from test_scenarios import db, event, login, vk_api
from test_scenarios import setup as setup

from app import broadcasts, clients

REAL_SEND = vk_api.send_message


def seed(sessions, user=77, contacted=True, **values):
    with sessions() as session:
        session.add(
            db.Client(
                user_id=user,
                first_name="Анна",
                last_name="Иванова",
                bot_contacted_at=clients.now() if contacted else None,
                profile_requested=False,
                **values,
            )
        )
        session.commit()


def prepare(client, **values):
    r = client.post(
        "/admin/api/broadcasts",
        json={
            "title": "Новости",
            "message": "Привет, {first_name} {last_name}!",
            **values,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def start(client, job):
    r = client.post(
        f"/admin/api/broadcasts/{job['id']}/start",
        json={"confirm_consent": True, "expected_count": job["total"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def tick():
    asyncio.run(broadcasts.worker_tick())


@pytest.fixture(autouse=True)
def no_real_vk(monkeypatch):
    monkeypatch.setattr(vk_api, "is_messages_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(vk_api, "call", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        vk_api, "upload_file_for_message", AsyncMock(return_value="photo-123_456")
    )


def test_auth_csrf_and_pages(setup):
    client, _, _ = setup
    for path in ("clients", "broadcasts"):
        assert client.get(f"/admin/api/{path}").status_code == 401
    login(client)
    for section in ("clients", "broadcasts", "dialogs"):
        r = client.get(f"/admin?section={section}")
        assert r.status_code == 200
        assert "Рассылки" in r.text
    client.headers.pop("X-CSRF-Token")
    assert client.post("/admin/api/clients/refresh").status_code == 403
    assert client.post("/admin/api/broadcasts", json={}).status_code == 403


def test_successful_send_is_audited_once_failure_never_is(setup):
    _, sessions, _ = setup
    asyncio.run(REAL_SEND(77, "Привет", random_id=41))
    asyncio.run(REAL_SEND(77, "Привет", random_id=41))
    vk_api.call.side_effect = vk_api.VkApiError("901: no permission")
    with pytest.raises(vk_api.VkApiError):
        asyncio.run(REAL_SEND(88, "Привет", random_id=42))
    with sessions() as session:
        assert session.query(db.BotMessage).count() == 1
        assert session.get(db.Client, 77).bot_contacted_at
        assert session.get(db.Client, 88) is None
    for ident in (-123, 0, 2_000_000_001, True):
        clients.record_outgoing(ident, 1)
    with sessions() as session:
        assert session.query(db.Client).count() == 1


def test_backfill_only_confirmed_outgoing_qualifies(setup):
    client, sessions, _ = setup
    with sessions() as session:
        session.add(db.Conversation(user_id=77, variables={"first_name": "Анна"}))
        session.add(db.Conversation(user_id=88, handoff=True))
        session.add(db.PromoDelivery(user_id=77, campaign_id=1))
        session.commit()
    clients.backfill_clients()
    clients.backfill_clients()
    with sessions() as session:
        assert session.query(db.Client).count() == 2
        assert session.get(db.Client, 77).first_name == "Анна"
        assert session.get(db.Client, 77).bot_contacted_at
        assert session.get(db.Client, 88).bot_contacted_at is None
    login(client)
    assert prepare(client)["total"] == 1


def test_client_search_pagination_detail_and_optout(setup):
    client, sessions, _ = setup
    for i in range(1, 33):
        seed(sessions, i)
    login(client)
    listing = client.get("/admin/api/clients").json()
    assert listing["total"] == 32 and len(listing["items"]) == 30
    assert len(client.get("/admin/api/clients?page=2").json()["items"]) == 2
    assert client.get("/admin/api/clients?q=21").json()["total"] == 1
    assert client.get("/admin/api/clients?q=Иванова").json()["total"] == 32
    assert client.get("/admin/api/clients?q=%25").json()["total"] == 0
    assert (
        client.get("/admin/api/clients/21").json()["client"]["vk_url"]
        == "https://vk.ru/id21"
    )
    assert client.get("/admin/api/clients/999").status_code == 404
    assert client.post("/admin/api/clients/21/unsubscribe").status_code == 200
    assert prepare(client)["total"] == 31


def test_vk_profile_contacts_photos_and_hidden_number_removal(setup):
    _, sessions, _ = setup
    seed(sessions)
    with sessions() as session:
        clients.save_profile(
            session,
            {
                "id": 77,
                "first_name": "Анна",
                "last_name": "Тест",
                "photo_100": "https://example.test/avatar.jpg",
                "mobile_phone": "+7 (999) 123-45-67",
            },
        )
        session.commit()
        c = session.get(db.Client, 77)
        assert c.phone_source == "vk" and c.phone == "+7 (999) 123-45-67"
        assert c.photo_url.startswith("https://")
        clients.save_profile(session, {"id": 77, "photo_100": "javascript:alert(1)"})
        session.commit()
        assert c.photo_url == "" and c.phone == "" and c.phone_source == ""
        session.add(db.Conversation(user_id=77, variables={"phone": "+79990000000"}))
        session.commit()
        clients.save_profile(session, {"id": 77, "mobile_phone": "<script>"})
        clients.save_profile(session, {"id": 999, "first_name": "Не клиент"})
        session.commit()
        assert c.phone_source == "dialog" and c.phone == "+79990000000"
        assert session.get(db.Client, 999) is None


def test_profile_refresh_is_batched_and_errors_are_visible(setup):
    client, sessions, _ = setup
    seed(sessions)
    login(client)
    assert client.post("/admin/api/clients/refresh").json()["queued"] == 1
    vk_api.call.return_value = [
        {"id": 77, "first_name": "Мария", "last_name": "Иванова"}
    ]
    tick()
    assert vk_api.call.call_args.kwargs["fields"] == "photo_100,contacts"
    assert client.get("/admin/api/clients/77").json()["client"]["first_name"] == "Мария"
    client.post("/admin/api/clients/refresh")
    vk_api.call.side_effect = vk_api.VkApiError("5: token")
    tick()
    assert (
        "5: token"
        in client.get("/admin/api/clients/77").json()["client"]["profile_error"]
    )


def test_draft_snapshot_filters_and_consent_start_guard(setup):
    client, sessions, sent = setup
    seed(sessions, 1)
    seed(sessions, 2, unsubscribed=True)
    seed(sessions, 3, deactivated=True)
    seed(sessions, 4, contacted=False)
    seed(sessions, 5)
    with sessions() as session:
        db.save_settings(session, {"operator_user_id": "5"})
        session.commit()
    login(client)
    job = prepare(client)
    assert job["total"] == 1 and job["status"] == "draft"
    assert "Анна Иванова" in job["preview"] and "Стоп" in job["preview"]
    tick()
    sent.assert_not_called()
    path = f"/admin/api/broadcasts/{job['id']}/start"
    assert client.post(path, json={"expected_count": 1}).status_code == 422
    assert (
        client.post(
            path, json={"confirm_consent": True, "expected_count": 2}
        ).status_code
        == 409
    )
    seed(sessions, 6)  # Does not alter already previewed audience.
    assert start(client, job)["total"] == 1
    assert (
        client.post(
            path, json={"confirm_consent": True, "expected_count": 1}
        ).status_code
        == 409
    )


def test_selected_audience_and_invalid_inputs(setup):
    client, sessions, _ = setup
    seed(sessions, 77)
    seed(sessions, 88)
    login(client)
    job = prepare(client, audience="selected", user_ids=[77, 77, 999])
    assert job["total"] == 1
    for extra in (
        {"message": " "},
        {"message": "x" * 4001},
        {"audience": "bad"},
        {"audience": "selected", "user_ids": []},
        {"media_id": "missing"},
    ):
        r = client.post(
            "/admin/api/broadcasts", json={"title": "a", "message": "b", **extra}
        )
        assert r.status_code == 422


def test_explicit_test_send_does_not_start_job(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    path = f"/admin/api/broadcasts/{job['id']}/test"
    assert client.post(path, json={"user_id": 88}).status_code == 200
    assert sent.call_args.args[0] == 88
    assert sent.call_args.args[1].startswith("[Тест рассылки]")
    assert client.get(f"/admin/api/broadcasts/{job['id']}").json()["status"] == "draft"
    vk_api.is_messages_allowed.return_value = False
    assert client.post(path, json={"user_id": 99}).status_code == 422
    assert sent.call_count == 1


def test_worker_sends_with_name_optout_and_finishes_once(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    tick()
    assert sent.call_count == 1
    args = sent.call_args
    assert args.args[:2] == (77, "Привет, Анна Иванова!" + broadcasts.FOOTER)
    payload = json.loads(args.kwargs["keyboard"]["buttons"][0][0]["action"]["payload"])
    assert payload["action"] == "broadcast_unsubscribe"
    tick()
    tick()
    assert sent.call_count == 1
    detail = client.get(f"/admin/api/broadcasts/{job['id']}").json()
    assert detail["status"] == "completed" and detail["counts"] == {"sent": 1}


@pytest.mark.parametrize("reason", ["permission", "optout", "deactivated", "operator"])
def test_exclusion_is_rechecked_at_delivery(setup, reason):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    if reason == "permission":
        vk_api.is_messages_allowed.return_value = False
    else:
        with sessions() as session:
            c = session.get(db.Client, 77)
            if reason == "optout":
                c.unsubscribed = True
            if reason == "deactivated":
                c.deactivated = True
            if reason == "operator":
                db.save_settings(session, {"operator_user_id": "77"})
            session.commit()
    tick()
    sent.assert_not_called()
    assert client.get(f"/admin/api/broadcasts/{job['id']}").json()["counts"] == {
        "skipped": 1
    }


def test_optout_while_permission_request_is_running(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)

    async def allowed(_):
        with sessions() as session:
            session.get(db.Client, 77).unsubscribed = True
            session.commit()
        return True

    vk_api.is_messages_allowed.side_effect = allowed
    tick()
    sent.assert_not_called()


def test_pause_resume_cancel_and_stale_send_cleanup(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    root = f"/admin/api/broadcasts/{job['id']}"
    assert client.post(root + "/pause").status_code == 200
    tick()
    sent.assert_not_called()
    assert client.post(root + "/resume").status_code == 200
    row_id, _ = broadcasts.claim_recipient()
    assert client.post(root + "/cancel").status_code == 200
    with sessions() as session:
        session.get(db.BroadcastRecipient, row_id).lease_until = (
            clients.now() - timedelta(seconds=1)
        )
        session.commit()
    tick()
    sent.assert_not_called()
    assert client.get(root).json()["counts"] == {"cancelled": 1}


def test_retry_keeps_random_id_and_rendered_message(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    sent.side_effect = vk_api.VkApiError("6: too many requests")
    tick()
    initial = sent.call_args
    tick()  # Backoff not yet due.
    assert sent.call_count == 1
    with sessions() as session:
        row = session.query(db.BroadcastRecipient).one()
        row.next_attempt_at = clients.now() - timedelta(seconds=1)
        session.get(db.Client, 77).first_name = "Другое имя"
        session.commit()
    sent.side_effect = None
    tick()
    assert sent.call_args == initial
    assert client.get(f"/admin/api/broadcasts/{job['id']}").json()["counts"] == {
        "sent": 1
    }


@pytest.mark.parametrize(
    "code,status",
    [
        (901, "skipped"),
        (902, "skipped"),
        (5, "pending"),
        (9, "pending"),
        (14, "pending"),
        (100, "failed"),
    ],
)
def test_vk_errors_are_classified_without_blind_retries(setup, code, status):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    sent.side_effect = vk_api.VkApiError(f"{code}: test")
    tick()
    d = client.get(f"/admin/api/broadcasts/{job['id']}").json()
    assert d["counts"] == {status: 1}
    if code in {5, 9, 14}:
        assert d["status"] == "paused"
        tick()
        assert sent.call_count == 1


def test_worker_lease_and_restart_recovery(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    assert broadcasts.claim_worker("first")
    assert not broadcasts.claim_worker("second")
    tick()
    sent.assert_not_called()
    broadcasts.release_worker("wrong-owner")
    assert not broadcasts.claim_worker("second")
    broadcasts.release_worker("first")
    row_id, _ = broadcasts.claim_recipient()
    with sessions() as session:
        row = session.get(db.BroadcastRecipient, row_id)
        row.payload = {"text": "Сохранённый текст", "attachment": "photo-123_456"}
        row.lease_until = clients.now() - timedelta(seconds=1)
        session.commit()
    tick()
    assert sent.call_args.args[1] == "Сохранённый текст"
    assert sent.call_args.kwargs["attachment"] == "photo-123_456"


def test_media_is_uploaded_but_missing_media_never_silently_dropped(setup, tmp_path):
    client, sessions, sent = setup
    seed(sessions)
    path = tmp_path / "file.jpg"
    path.write_bytes(b"test-image")
    with sessions() as session:
        session.add(
            db.MediaAsset(
                id="asset",
                filename="file.jpg",
                content_type="image/jpeg",
                path=str(path),
            )
        )
        session.commit()
    login(client)
    job = prepare(client, media_id="asset")
    start(client, job)
    tick()
    assert sent.call_args.kwargs["attachment"] == "photo-123_456"
    sent.reset_mock()
    job2 = prepare(client, media_id="asset")
    start(client, job2)
    path.unlink()
    tick()
    sent.assert_not_called()
    assert client.get(f"/admin/api/broadcasts/{job2['id']}").json()["counts"] == {
        "failed": 1
    }


def test_stop_works_with_disabled_chat_and_preserves_handoff(setup):
    client, sessions, sent = setup
    seed(sessions)
    with sessions() as session:
        db.save_settings(session, {"chat_enabled": "false"})
        session.add(
            db.Conversation(
                user_id=77,
                handoff=True,
                assigned_operator_id=99,
                node_id="collect",
                variables={"phone": "+79990000000"},
            )
        )
        session.commit()
    assert client.post("/vk/callback", json=event("Стоп")).status_code == 200
    client.post("/vk/callback", json=event("Стоп"))
    assert sent.call_count == 1
    with sessions() as session:
        assert session.get(db.Client, 77).unsubscribed
        row = session.get(db.Conversation, 77)
        assert (
            row.handoff and row.assigned_operator_id == 99 and row.node_id == "collect"
        )
    client.post("/vk/callback", json=event("Подписаться на рассылку", number=2))
    with sessions() as session:
        assert not session.get(db.Client, 77).unsubscribed
    client.post(
        "/vk/callback",
        json=event("", number=3, payload={"action": "broadcast_unsubscribe"}),
    )
    with sessions() as session:
        assert session.get(db.Client, 77).unsubscribed


def test_permission_events_do_not_resubscribe_customer(setup):
    client, sessions, _ = setup
    seed(sessions)
    payload = {
        "type": "message_deny",
        "group_id": 123,
        "secret": "test-secret",
        "object": {"user_id": 77},
    }
    client.post("/vk/callback", json=payload)
    payload["type"] = "message_allow"
    client.post("/vk/callback", json=payload)
    with sessions() as session:
        c = session.get(db.Client, 77)
        assert c.unsubscribed and c.messages_allowed
    payload["group_id"] = 456
    payload["type"] = "message_deny"
    client.post("/vk/callback", json=payload)
    with sessions() as session:
        assert session.get(db.Client, 77).messages_allowed


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_during_permission_check_prevents_send(setup, action):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)

    async def permission(_):
        with sessions() as session:
            session.get(db.Broadcast, job["id"]).status = (
                "paused" if action == "pause" else "cancelled"
            )
            session.commit()
        return True

    vk_api.is_messages_allowed.side_effect = permission
    tick()
    sent.assert_not_called()
    with sessions() as session:
        assert session.query(db.BroadcastRecipient).one().status == (
            "pending" if action == "pause" else "cancelled"
        )


def test_transient_failures_stop_after_four_attempts(setup):
    client, sessions, sent = setup
    seed(sessions)
    login(client)
    job = prepare(client)
    start(client, job)
    sent.side_effect = vk_api.VkApiError("6: rate limit")
    for _ in range(4):
        with sessions() as session:
            session.query(db.BroadcastRecipient).one().next_attempt_at = (
                clients.now() - timedelta(seconds=1)
            )
            session.commit()
        tick()
    tick()
    assert sent.call_count == 4
    d = client.get(f"/admin/api/broadcasts/{job['id']}").json()
    assert d["status"] == "completed" and d["counts"] == {"failed": 1}


def test_stop_persists_even_if_acknowledgment_is_rejected(setup):
    client, sessions, sent = setup
    seed(sessions)
    sent.side_effect = vk_api.VkApiError("901: forbidden")
    assert client.post("/vk/callback", json=event("Стоп")).status_code == 200
    with sessions() as session:
        assert session.get(db.Client, 77).unsubscribed
    login(client)
    r = client.post("/admin/api/broadcasts", json={"title": "a", "message": "b"})
    assert r.status_code == 422


def test_unsubscribed_user_is_not_sent_even_an_admin_test(setup):
    client, sessions, sent = setup
    seed(sessions)
    seed(sessions, 88, unsubscribed=True)
    login(client)
    job = prepare(client)
    r = client.post(f"/admin/api/broadcasts/{job['id']}/test", json={"user_id": 88})
    assert r.status_code == 422
    sent.assert_not_called()


def test_invalid_profile_photo_is_ignored():
    for url in (
        "https://[broken",
        "javascript:alert(1)",
        "http://example.test/p",
        "https://user:pass@example.test/p",
    ):
        assert clients.safe_photo(url) == ""


def test_additive_schema_init_preserves_queue_and_optout(setup):
    client, sessions, _ = setup
    seed(sessions)
    seed(sessions, 88, unsubscribed=True)
    login(client)
    job = prepare(client)
    start(client, job)
    db.init_db()
    clients.backfill_clients()
    with sessions() as session:
        assert session.get(db.Client, 88).unsubscribed
        assert session.get(db.Broadcast, job["id"]).status == "queued"
        assert session.query(db.BroadcastRecipient).count() == 1
