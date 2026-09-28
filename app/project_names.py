"""Read public VK names without treating a name lookup as token verification."""

import httpx

from . import projects
from .config import get_settings


class GroupLookupError(ValueError):
    pass


async def fetch_group(project, *, owner=False):
    if not project.token:
        raise GroupLookupError(
            "Сначала сохраните токен сообщества в настройках проекта."
        )
    with projects.project_scope(project):
        data = {"access_token": project.token, "v": get_settings().vk_api_version}
    # The explicit lookup is for a public NAME, never proof of token ownership.
    # Checking the token still uses the implicit owner response without group_id.
    if not owner:
        data["group_id"] = str(project.group_id)
    try:
        with projects.project_scope(project):
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(
                    "https://api.vk.com/method/groups.getById", data=data
                )
                response.raise_for_status()
                payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise GroupLookupError(
            "VK не ответил. Повторите получение названия позже."
        ) from None
    if not isinstance(payload, dict):
        raise GroupLookupError("VK вернул неожиданный ответ. Попробуйте ещё раз.")
    if payload.get("error"):
        error = payload["error"]
        code = error.get("error_code") if isinstance(error, dict) else None
        code = code if type(code) is int else None
        explanations = {
            5: "Токен недействителен или отозван. Сохраните новый ключ сообщества.",
            27: "Проверьте сохранённый ключ доступа сообщества.",
            15: "VK отказал в доступе. Проверьте ключ сообщества и его права.",
            6: "Слишком много запросов к VK. Повторите через минуту.",
            29: "VK временно ограничил запросы. Повторите позже.",
            100: "VK не принял параметры. Проверьте числовой ID сообщества и версию API.",
        }
        # Do not echo API descriptions/params: they can contain access tokens.
        label = f"Ошибка VK {code}. " if type(code) is int else "Ошибка VK. "
        raise GroupLookupError(
            label + explanations.get(code, "Проверьте ключ и попробуйте позже.")
        )
    result = payload.get("response")
    if isinstance(result, dict):
        result = result.get("groups", [result] if "id" in result else [])
    if isinstance(result, list):
        for group in result:
            if isinstance(group, dict) and str(group.get("id")) == str(
                project.group_id
            ):
                return group
    raise GroupLookupError(
        "VK не подтвердил принадлежность токена этой группе. Проверьте ID и ключ."
        if owner
        else "VK не вернул указанную группу. Проверьте её числовой ID."
    )


def save_name(project, group, *, force=False):
    name = group.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GroupLookupError(
            "VK не вернул название группы. Его можно ввести вручную в настройках проекта."
        )
    return projects.update_fetched_name(project, name.strip()[:120], force=force)
