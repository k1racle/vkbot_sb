from unittest.mock import AsyncMock

from test_projects import project_env as project_env, login, create, prefix, session_for
from app import db, vk_api


def test_diagnostics_contacts_and_journey_stay_in_project(project_env, monkeypatch):
    env = project_env
    login(env.client)
    first = create(env.client, "First", 456)
    second = create(env.client, "Second", 789)
    for project, email in (
        (first, "first@example.com"),
        (second, "second@example.com"),
    ):
        with session_for(project) as session:
            session.add(db.Client(user_id=77, email=email))
            session.add(
                db.DialogEvent(
                    event_key="same-key",
                    user_id=77,
                    journey=[{"scenario": project.name}],
                )
            )
            session.commit()
    env.client.post(
        f"/vk/callback/{first.id}",
        json={
            "group_id": 456,
            "secret": first.secret,
            "type": "video_comment_new",
            "object": {},
        },
    )
    request = AsyncMock(side_effect=vk_api.VkApiError("5: denied"))
    monkeypatch.setattr(vk_api, "_request", request)
    report = env.client.post(prefix(first) + "/api/vk-diagnostics").json()
    assert report["last_callback"]["type"] == "video_comment_new"
    assert all(call.args[1] == first.token for call in request.call_args_list)
    request.reset_mock()
    report = env.client.post(prefix(second) + "/api/vk-diagnostics").json()
    assert report["last_callback"] is None
    assert all(call.args[1] == second.token for call in request.call_args_list)
    for project, expected in (
        (first, "first@example.com"),
        (second, "second@example.com"),
    ):
        data = env.client.get(prefix(project) + "/api/clients/77").json()
        assert data["client"]["email"] == expected
        assert data["events"][0]["journey"] == [{"scenario": project.name}]
