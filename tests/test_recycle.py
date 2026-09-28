"""Names and recoverable deletion; no requests to VK or production databases."""

import asyncio

import pytest

from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    callback,
    comment,
    configure,
    db,
    login,
    message,
    prefix,
    projects,
    session_for,
)
from app import broadcasts, vk_api


def add_campaign(project, post_id=0):
    with session_for(project) as session:
        item = db.Campaign(
            title="Старая акция", post_id=post_id, promo_message="Подарок", enabled=True
        )
        session.add(item)
        session.commit()
        return item.id


def add_broadcast(project, state="queued"):
    with session_for(project) as session:
        item = db.Broadcast(
            id="mail",
            title="Рассылка",
            message="Текст",
            status=state,
            consent_confirmed=True,
        )
        session.add(item)
        session.add(
            db.BroadcastRecipient(broadcast_id="mail", user_id=77, status="pending")
        )
        session.commit()


@pytest.mark.parametrize(
    "state", ["draft", "running", "paused", "completed", "cancelled"]
)
def test_rename_broadcast_only_changes_internal_title(two_projects, state):
    env, first, second = two_projects
    add_broadcast(first, state)
    add_broadcast(second, state)
    csrf = login(env.client)
    headers = {"X-CSRF-Token": csrf}
    url = prefix(first) + "/api/broadcasts/mail/title"
    before_calls = list(env.network.calls)
    assert (
        env.client.patch(
            url, json={"title": "Новое"}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    for invalid in ("", "  ", "x" * 121):
        assert (
            env.client.patch(url, json={"title": invalid}, headers=headers).status_code
            == 422
        )
    title = "Тест рассылки из админки бота"
    response = env.client.patch(url, json={"title": f"  {title}  "}, headers=headers)
    assert response.status_code == 200
    with session_for(first) as session:
        job = session.get(db.Broadcast, "mail")
        assert (job.title, job.message, job.status) == (title, "Текст", state)
        assert job.consent_confirmed
        assert session.query(db.BroadcastRecipient).one().status == "pending"
    with session_for(second) as session:
        assert session.get(db.Broadcast, "mail").title == "Рассылка"
    assert env.network.calls == before_calls
    env.client.post(prefix(first) + "/api/broadcasts/mail/delete", headers=headers)
    assert (
        env.client.patch(url, json={"title": title}, headers=headers).status_code == 404
    )


@pytest.mark.parametrize(
    "refresh,custom,expected",
    [
        (False, False, "SARKISIAN BRAND"),
        (False, True, "Моя группа"),
        (True, True, "SARKISIAN BRAND"),
    ],
)
def test_vk_group_name_preserves_custom_unless_requested(
    two_projects, refresh, custom, expected
):
    env, first, second = two_projects
    name = "Моя группа" if custom else f"VK {first.group_id}"
    first = configure(env.client, first, name=name)
    env.network.replies["groups.getById"] = {
        "response": {"groups": [{"id": first.group_id, "name": "SARKISIAN BRAND"}]}
    }
    response = env.client.post(
        f"/projects/{first.id}/check", data={"refresh_name": "1" if refresh else "0"}
    )
    assert response.status_code == 200
    assert projects.get_project(first.id).name == expected
    assert projects.get_project(second.id).name == second.name
    html = env.client.get(prefix(first)).text
    assert f">{expected}</option>" in html
    assert first.token not in html
    assert [c["method"] for c in env.network.calls] == ["groups.getById"]


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"error_code": 5}},
        {"response": {"groups": [{"id": 999, "name": "Wrong"}]}},
        {"response": {"groups": [{"id": 456}]}},
    ],
)
def test_name_refresh_failure_does_not_erase_name(two_projects, reply):
    env, first, _ = two_projects
    env.network.replies["groups.getById"] = reply
    response = env.client.post(
        f"/projects/{first.id}/check", data={"refresh_name": "1"}
    )
    assert response.status_code in {200, 422}
    assert projects.get_project(first.id).name == first.name


def test_project_delete_confirmation_isolation_stale_context_and_restore(two_projects):
    env, first, second = two_projects
    ident = add_campaign(first)
    add_broadcast(first)
    with session_for(first) as session:
        session.add(db.Client(user_id=77, unsubscribed=True, profile_requested=True))
        session.add(db.PromoDelivery(user_id=77, campaign_id=ident))
        session.commit()
    endpoint = f"/projects/{first.id}/delete"
    assert (
        env.client.post(
            endpoint, data={"confirmation": str(second.group_id)}
        ).status_code
        == 422
    )
    assert projects.get_project(first.id).enabled
    assert (
        env.client.post(
            endpoint, data={"confirmation": str(first.group_id)}
        ).status_code
        == 200
    )
    assert projects.get_project(first.id) is None
    assert projects.get_project(second.id).enabled
    archived = projects.get_project(first.id, include_deleted=True)
    assert archived.is_deleted and not archived.enabled
    assert projects.get_project_by_group_id(first.group_id) is None
    assert env.client.get(prefix(first) + "/api/clients").status_code == 404
    assert env.client.get(prefix(second) + "/api/clients").status_code == 200
    assert callback(env.client, first, comment(first)).text == "ok"
    assert callback(env.client, first, message(first)).text == "ok"
    with projects.project_scope(first):
        asyncio.run(broadcasts.worker_tick())
        with pytest.raises(vk_api.VkApiError):
            asyncio.run(vk_api.send_message(77, "Must not send", random_id=1))
    assert not env.network.calls
    with session_for(archived) as session:
        assert session.query(db.PromoDelivery).count() == 1
        assert session.get(db.Client, 77).unsubscribed
        assert not session.get(db.Client, 77).profile_requested
        assert session.get(db.Broadcast, "mail").status == "cancelled"
        assert session.query(db.BroadcastRecipient).one().status == "cancelled"
    projects.init_registry()  # Existing ENV must not resurrect a removed group.
    assert projects.get_project(first.id) is None
    assert env.client.post(f"/projects/{first.id}/restore").status_code == 200
    restored = projects.get_project(first.id)
    assert restored and not restored.enabled and restored.token == first.token
    with session_for(restored) as session:
        assert session.get(db.Broadcast, "mail").status == "cancelled"
    response = env.client.post(
        f"/projects/{first.id}/settings", data={"name": first.name, "enabled": "1"}
    )
    assert response.status_code == 200
    with projects.project_scope(projects.get_project(first.id)):
        assert broadcasts.claim_recipient() is None


def test_deleted_legacy_not_reimported_and_callbacks_validate_secret(project_env):
    env = project_env
    login(env.client)
    first = projects.list_projects()[0]
    env.client.post(
        f"/projects/{first.id}/delete", data={"confirmation": str(first.group_id)}
    )
    projects.init_registry()
    assert projects.list_projects() == []
    assert len(projects.list_projects(include_deleted=True)) == 1
    assert (
        env.client.get("/admin", follow_redirects=False).headers["location"]
        == "/projects"
    )
    assert env.client.post("/vk/callback", json=comment(first)).text == "ok"
    wrong = comment(first)
    wrong["secret"] = "wrong"
    assert env.client.post("/vk/callback", json=wrong).text == "invalid secret"
    assert (
        env.client.post(
            "/projects", data={"name": "Duplicate", "group_id": first.group_id}
        ).status_code
        == 422
    )
    assert not env.network.calls


def test_campaign_delete_frees_post_but_keeps_id_and_history(two_projects):
    env, first, second = two_projects
    ident = add_campaign(first)
    other = add_campaign(second)
    with session_for(first) as session:
        session.add(db.PromoDelivery(user_id=77, campaign_id=ident))
        session.add(
            db.PendingGift(
                id="gift",
                user_id=77,
                campaign_id=ident,
                event_key="old-comment",
                active_key=f"77:{ident}",
                awaiting_subscription=True,
            )
        )
        session.commit()
    assert (
        env.client.post(prefix(first) + f"/campaigns/{ident}/delete").status_code == 200
    )
    with session_for(first) as session:
        row = session.get(db.Campaign, ident)
        assert (
            row.is_deleted
            and not row.enabled
            and row.post_id == -ident
            and row.archived_post_id == 0
        )
        assert session.query(db.PromoDelivery).count() == 1
        assert session.get(db.PendingGift, "gift").status == "cancelled"
        assert session.get(db.PendingGift, "gift").active_key is None
    assert env.client.get(prefix(first) + "/api/trash").json()["items"] == [
        {"kind": "campaign", "id": ident, "title": "Старая акция"}
    ]
    assert env.client.get(prefix(second) + "/api/trash").json()["items"] == []
    # An old form can neither enable nor test-send a deleted campaign.
    env.client.post(prefix(first) + f"/campaigns/{ident}/toggle")
    env.client.post(
        prefix(first) + "/test-send", data={"campaign_id": ident, "user_id": 77}
    )
    with session_for(first) as session:
        assert not session.get(db.Campaign, ident).enabled
    replacement = add_campaign(first)
    assert replacement != ident  # IDs referenced in delivery history are never reused.
    restore = prefix(first) + f"/api/trash/campaign/{ident}/restore"
    assert env.client.post(restore).status_code == 409
    env.client.post(prefix(first) + f"/campaigns/{replacement}/delete")
    assert env.client.post(restore).status_code == 200
    with session_for(first) as session:
        row = session.get(db.Campaign, ident)
        assert row.post_id == 0 and not row.is_deleted and not row.enabled
        assert session.get(db.PendingGift, "gift").status == "cancelled"
    with session_for(second) as session:
        assert session.get(db.Campaign, other).enabled
    assert not env.network.calls


def test_scenario_delete_revision_and_restore_no_activation(two_projects):
    env, first, second = two_projects
    flow = env.client.post(prefix(first) + "/api/scenarios").json()
    ident = flow["id"]
    other = env.client.post(prefix(second) + "/api/scenarios").json()
    endpoint = prefix(first) + f"/api/scenarios/{ident}/delete"
    assert env.client.post(endpoint, json={"revision": 100}).status_code == 409
    assert (
        env.client.post(endpoint, json={"revision": flow["revision"]}).status_code
        == 200
    )
    assert env.client.get(prefix(first) + "/api/scenarios").json()["scenarios"] == []
    assert (
        len(env.client.get(prefix(second) + "/api/scenarios").json()["scenarios"]) == 1
    )
    assert (
        env.client.post(
            prefix(first) + f"/api/scenarios/{ident}/publish",
            json={"revision": flow["revision"]},
        ).status_code
        == 404
    )
    assert (
        env.client.post(
            prefix(first) + f"/api/trash/scenario/{ident}/restore"
        ).status_code
        == 200
    )
    with session_for(first) as session:
        restored = session.get(db.Scenario, ident)
        assert (
            not restored.is_deleted
            and not restored.active
            and restored.revision == flow["revision"] + 2
        )
    assert (
        env.client.post(
            prefix(second) + f"/api/trash/scenario/{other['id']}/restore"
        ).status_code
        == 404
    )
    assert not env.network.calls


@pytest.mark.parametrize("state", ["draft", "queued", "running", "paused", "completed"])
def test_broadcast_delete_cancels_without_erasing_and_restore_never_sends(
    two_projects, state
):
    env, first, _ = two_projects
    add_broadcast(first, state)
    response = env.client.post(prefix(first) + "/api/broadcasts/mail/delete")
    assert response.status_code == 200
    assert env.client.get(prefix(first) + "/api/broadcasts").json()["items"] == []
    assert (
        env.client.post(prefix(first) + "/api/broadcasts/mail/resume").status_code
        == 404
    )
    assert (
        env.client.post(
            prefix(first) + "/api/broadcasts/mail/test", json={"user_id": 77}
        ).status_code
        == 404
    )
    assert (
        env.client.post(prefix(first) + "/api/trash/broadcast/mail/restore").status_code
        == 200
    )
    with session_for(first) as session:
        assert session.get(db.Broadcast, "mail").status == (
            "completed" if state == "completed" else "cancelled"
        )
        assert session.query(db.BroadcastRecipient).one().status == "cancelled"
    with projects.project_scope(first):
        assert broadcasts.claim_recipient() is None
    assert not env.network.calls


def test_delete_and_restore_require_login_csrf_and_are_scoped(two_projects):
    env, first, second = two_projects
    ident = add_campaign(first)
    flow = env.client.post(prefix(first) + "/api/scenarios").json()
    add_broadcast(first)
    for method in ("delete", "restore"):
        assert (
            env.client.post(
                prefix(second) + f"/api/trash/campaign/{ident}/restore"
            ).status_code
            == 404
        )
    env.client.headers.pop("X-CSRF-Token")
    urls = [
        f"/projects/{first.id}/delete",
        f"/projects/{first.id}/restore",
        prefix(first) + f"/campaigns/{ident}/delete",
        prefix(first) + f"/api/scenarios/{flow['id']}/delete",
        prefix(first) + "/api/broadcasts/mail/delete",
        prefix(first) + f"/api/trash/campaign/{ident}/restore",
    ]
    for url in urls:
        assert env.client.post(url, json={"revision": 0}).status_code == 403
    env.client.cookies.clear()
    assert env.client.get(prefix(first) + "/api/trash").status_code == 401
    assert (
        env.client.post(
            f"/projects/{first.id}/delete", follow_redirects=False
        ).status_code
        == 303
    )
    assert not env.network.calls
