"""Read-only VK checks and a minimal authenticated Callback API receipt."""

import json
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import clients, db, projects, vk_api
from .config import get_settings
from .scenario_api import authorize

router = APIRouter(prefix="/admin/api", dependencies=[Depends(authorize)])
EVENTS = {
    "message_new": "Входящие сообщения",
    "wall_reply_new": "Комментарии к постам",
    "video_comment_new": "Комментарии к видео",
    "group_join": "Вступление в сообщество",
    "message_allow": "Разрешение сообщений",
    "message_deny": "Запрет сообщений",
    "message_reply": "Ответ менеджера",
}


def record_callback(payload, *, paused=False):
    """Call only after validating both the secret and group ID; never store payloads."""
    kind = str(payload.get("type", ""))[:80]
    reason = "Событие получено и передано обработчику"
    if kind == "confirmation":
        reason = "Запрос подтверждения сервера"
    elif paused:
        reason = "Проект на паузе; автоматические ответы отключены"
    elif kind not in EVENTS:
        reason = "Этот тип события не обрабатывается ботом"
    elif kind in {"wall_reply_new", "video_comment_new"}:
        from .comments import normalize_comment

        if normalize_comment(payload, get_settings().vk_group_id) is None:
            reason = "Комментарий пропущен: чужая публикация, автор-сообщество или неполные данные"
    value = json.dumps(
        {"date": clients.now().isoformat() + "Z", "type": kind, "reason": reason},
        ensure_ascii=False,
    )
    with db.SessionLocal() as session:
        insert = (
            pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
        )
        statement = insert(db.BotSetting).values(key="last_vk_callback", value=value)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["key"], set_={"value": value}
            )
        )
        session.commit()


def same_endpoint(actual, expected):
    try:
        a, b = urlsplit(actual), urlsplit(expected)
    except ValueError:
        return False
    return (
        (a.scheme, a.netloc, a.path.rstrip("/"))
        == (b.scheme, b.netloc, b.path.rstrip("/"))
        and not a.query
        and not a.fragment
    )


async def connection_report(project, base_url):
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    async def query(name, method, token, **params):
        try:
            return await vk_api._request(method, token, **params)
        except vk_api.VkApiError as error:
            explanations = {
                5: "Токен недействителен или отозван",
                27: "Недействительный ключ сообщества",
                15: "Недостаточно прав",
                7: "Недостаточно прав",
                6: "Слишком много запросов; повторите позже",
                29: "VK временно ограничил запросы",
            }
            add(
                name,
                "error",
                explanations.get(error.code, "VK отклонил проверку")
                + (f" (код {error.code})" if error.code is not None else ""),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            add(
                name,
                "error",
                "Не удалось получить корректный ответ VK. Повторите проверку позже.",
            )
        return None

    with db.SessionLocal() as session:
        saved = db.read_settings(session).get("last_vk_callback", "")
    try:
        last = json.loads(saved) if saved else None
    except ValueError:
        last = None
    if not project:
        return {"checks": [], "last_callback": last}
    add(
        "Проект",
        "ok" if project.enabled else "warning",
        "Включён" if project.enabled else "На паузе: автоматические ответы отключены",
    )
    expected = base_url.rstrip("/") + f"/vk/callback/{project.id}"
    if not project.token:
        add(
            "Токен сообщества",
            "error",
            "Не задан. Сохраните токен в настройках проекта.",
        )
    else:
        owner = await query("Токен сообщества", "groups.getById", project.token)
        if owner is not None:
            groups = (
                owner.get("groups", [owner] if "id" in owner else [])
                if isinstance(owner, dict)
                else owner
            )
            matches = isinstance(groups, list) and any(
                isinstance(g, dict) and g.get("id") == project.group_id for g in groups
            )
            add(
                "Принадлежность токена",
                "ok" if matches else "error",
                "Ключ принадлежит этому сообществу"
                if matches
                else "VK не подтвердил, что ключ принадлежит этому сообществу",
            )
        permissions = await query(
            "Права сообщества", "groups.getTokenPermissions", project.token
        )
        if isinstance(permissions, dict):
            granted = [
                str(p.get("name"))
                for p in permissions.get("permissions", [])
                if isinstance(p, dict) and p.get("setting") == 1
            ]
            add(
                "Права сообщества",
                "ok" if "messages" in granted else "warning",
                "Доступные права: "
                + (", ".join(granted) or "не получены")
                + ("" if "messages" in granted else ". Проверьте право на сообщения."),
            )
        servers = await query(
            "Callback API",
            "groups.getCallbackServers",
            project.token,
            group_id=project.group_id,
        )
        if isinstance(servers, dict):
            allowed_urls = [expected]
            if project.is_legacy:
                allowed_urls.append(base_url.rstrip("/") + "/vk/callback")
            matching = [
                s
                for s in servers.get("items", [])
                if isinstance(s, dict)
                and any(
                    same_endpoint(str(s.get("url", "")), url) for url in allowed_urls
                )
            ]
            if not matching:
                add(
                    "Адрес Callback API",
                    "error",
                    "В VK не найден сервер с адресом " + expected,
                )
            for server in matching:
                add(
                    "Сервер Callback API",
                    "ok" if server.get("status") == "ok" else "warning",
                    "Статус VK: " + str(server.get("status", "неизвестен")),
                )
                if "secret_key" in server:
                    correct = (
                        bool(project.callback_secret)
                        and server["secret_key"] == project.callback_secret
                    )
                    add(
                        "Секрет Callback API",
                        "ok" if correct else "error",
                        "Совпадает"
                        if correct
                        else "Не совпадает с настройками проекта",
                    )
                data = await query(
                    "События Callback API",
                    "groups.getCallbackSettings",
                    project.token,
                    group_id=project.group_id,
                    server_id=server["id"],
                )
                if isinstance(data, dict):
                    events = data.get("events", {})
                    for event, label in EVENTS.items():
                        enabled = events.get(event) in (1, True)
                        add(
                            label,
                            "ok" if enabled else "warning",
                            "Включено"
                            if enabled
                            else "Выключено в Callback API — включите, если используете эту функцию",
                        )
    if not project.video_token:
        add("Ответы под видео", "warning", "Пользовательский токен не задан")
    else:
        mask = await query(
            "Токен для видео", "account.getAppPermissions", project.video_token
        )
        if type(mask) is int:
            add(
                "Доступ к видео",
                "ok" if mask & 16 else "error",
                "Право video предоставлено"
                if mask & 16
                else "У токена нет права video",
            )
        group = await query(
            "Администратор сообщества",
            "groups.getById",
            project.video_token,
            group_id=project.group_id,
        )
        if group is not None:
            rows = (
                group.get("groups", [group] if "id" in group else [])
                if isinstance(group, dict)
                else group
            )
            admin = isinstance(rows, list) and any(
                g.get("id") == project.group_id and g.get("is_admin") == 1
                for g in rows
                if isinstance(g, dict)
            )
            add(
                "Администратор сообщества",
                "ok" if admin else "warning",
                "Права администратора подтверждены"
                if admin
                else "VK не подтвердил права администратора у владельца токена",
            )
    return {
        "checks": checks,
        "last_callback": last,
        "callback_url": expected,
        "checked_at": clients.now().isoformat() + "Z",
    }


@router.post("/vk-diagnostics")
async def diagnose(request: Request):
    selected = projects.current_project.get()
    project = projects.get_project(selected.id) if selected else None
    base = get_settings().public_base_url or str(request.base_url).rstrip("/")
    return await connection_report(project, base)
