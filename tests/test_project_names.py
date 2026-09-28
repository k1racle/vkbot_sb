"""Names are scoped, safely refreshed and separate from token verification."""

import asyncio

import pytest

from test_projects import (
    project_env as project_env,
    two_projects as two_projects,
    configure,
    projects,
)
from app import project_names


@pytest.mark.parametrize("shape", ["object", "list", "single"])
def test_refresh_name_uses_group_id_and_updates_dropdown(two_projects, shape):
    env, first, second = two_projects
    first = configure(env.client, first, name=f"VK {first.group_id}")
    group = {"id": first.group_id, "name": "SARKISIAN BRAND"}
    env.network.replies["groups.getById"] = {
        "response": {"groups": [group]}
        if shape == "object"
        else [group]
        if shape == "list"
        else group
    }
    response = env.client.post(f"/projects/{first.id}/name", data={"automatic": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["project"]["name"] == "SARKISIAN BRAND"
    assert env.network.calls[-1]["params"]["group_id"] == str(first.group_id)
    assert env.network.calls[-1]["token"] == first.token
    html = env.client.get("/projects").text
    assert ">SARKISIAN BRAND</option>" in html
    card = next(
        line for line in html.splitlines() if f'id="project-{first.id}"' in line
    )
    assert 'data-auto-name="0"' in card
    assert projects.get_project(second.id).name == second.name
    assert first.token not in response.text


def test_post_check_renders_fresh_name_without_an_extra_reload(two_projects):
    env, first, _ = two_projects
    env.network.replies["groups.getById"] = {
        "response": {"groups": [{"id": first.group_id, "name": "Новое имя"}]}
    }
    response = env.client.post(
        f"/projects/{first.id}/check", data={"refresh_name": "1"}
    )
    assert response.status_code == 200
    assert ">Новое имя</option>" in response.text
    assert (
        "group_id" not in env.network.calls[-1]["params"]
    )  # Ownership check stays separate.


def test_auto_name_preserves_custom_and_manual_force_is_explicit(two_projects):
    env, first, _ = two_projects
    response = env.client.post(f"/projects/{first.id}/name", data={"automatic": "1"})
    assert response.status_code == 200 and not env.network.calls
    env.network.replies["groups.getById"] = {
        "response": {"groups": [{"id": first.group_id, "name": "Имя VK"}]}
    }
    response = env.client.post(f"/projects/{first.id}/name")
    assert response.status_code == 200
    assert projects.get_project(first.id).name == "Имя VK"


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"error_code": 5, "error_msg": "TOKEN_SECRET"}},
        {"error": {"error_code": {"bad": "shape"}}},
        {"response": {"groups": [{"id": 999, "name": "Wrong"}]}},
        {"response": {"groups": [{"id": 456, "name": ""}]}},
        {"response": None},
        [],
    ],
)
def test_lookup_failures_do_not_erase_name_or_expose_tokens(two_projects, response):
    env, first, _ = two_projects
    env.network.replies["groups.getById"] = response
    result = env.client.post(f"/projects/{first.id}/name")
    assert result.status_code == 422, result.text
    assert projects.get_project(first.id).name == first.name
    assert "TOKEN_SECRET" not in result.text and first.token not in result.text


def test_fetch_race_does_not_overwrite_new_name_or_token(two_projects):
    _, first, _ = two_projects
    renamed = projects.update_project(first.id, name="Ручное изменение")
    result = project_names.save_name(first, {"name": "VK название"}, force=True)
    assert result.name == renamed.name
    projects.update_project(first.id, name=first.name, token="different-token")
    result = project_names.save_name(first, {"name": "VK название"}, force=True)
    assert result.name == first.name and result.token == "different-token"


def test_name_lookup_auth_csrf_archived_and_blank_token(two_projects):
    env, first, second = two_projects
    env.client.headers.pop("X-CSRF-Token")
    assert env.client.post(f"/projects/{first.id}/name").status_code == 403
    env.client.cookies.clear()
    assert env.client.post(f"/projects/{first.id}/name").status_code == 401
    projects.delete_project(first.id, str(first.group_id))
    with pytest.raises(ValueError):
        project_names.save_name(first, {"name": "Не оживлять"}, force=True)
    blank = projects.create_project("Без ключа", 890)
    with pytest.raises(project_names.GroupLookupError, match="токен"):
        asyncio.run(project_names.fetch_group(blank))
    assert projects.get_project(second.id).name == second.name
