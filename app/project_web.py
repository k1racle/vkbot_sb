"""Explicit project routes; no mutable 'selected project' cookie or global DB."""

import re
import secrets
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

from . import projects
from .clients import defer_outgoing_audits
from .config import get_settings

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


async def protect_admin_form(request: Request):
    """Protect legacy HTML forms too, not only the newer JSON endpoints."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not request.scope["path"].startswith(("/admin", "/projects")):
        return
    if not request.session.get("admin_authenticated"):
        return  # Route-specific auth decides HTML redirect versus API 401.
    expected = request.session.get("csrf", "")
    actual = request.headers.get("X-CSRF-Token", "")
    if not actual and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        actual = str((await request.form()).get("csrf_token", ""))
    if (
        not expected
        or not actual.isascii()
        or not secrets.compare_digest(expected, actual)
    ):
        raise HTTPException(403, "Обновите страницу и повторите действие")


class ProjectMiddleware:
    """Bind one immutable project context for the entire ASGI request lifetime."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        is_admin = path.startswith(("/admin", "/p/"))
        if is_admin and not scope.get("session", {}).get("admin_authenticated"):
            response = (
                JSONResponse({"detail": "Войдите в панель управления"}, status_code=401)
                if "/admin/api" in path
                else RedirectResponse("/login", status_code=303)
            )
            return await response(scope, receive, send)
        project = None
        prefix = ""
        callback = path.startswith("/vk/callback")
        match = re.fullmatch(r"/p/([1-9][0-9]{0,9})/admin(.*)", path)
        callback_match = re.fullmatch(r"/vk/callback/([1-9][0-9]{0,9})", path)
        if match:
            project = (
                projects.get_project(int(match[1]))
                if int(match[1]) <= 2147483647
                else None
            )
            prefix = f"/p/{match[1]}"
            scope = dict(scope, path=f"/admin{match[2]}")
        elif callback_match:
            project = (
                projects.get_project(int(callback_match[1]), include_deleted=True)
                if int(callback_match[1]) <= 2147483647
                else None
            )
            scope = dict(scope, path="/vk/callback")
        elif path == "/vk/callback" or path == "/admin" or path.startswith("/admin/"):
            # Old bookmarks/forms are permanently bound to the imported group,
            # never to whichever group was opened in a different browser tab.
            project = next(
                (
                    p
                    for p in projects.list_projects(include_deleted=callback)
                    if p.is_legacy
                ),
                None,
            )
        elif path.startswith(("/p/", "/vk/callback")):
            return await PlainTextResponse("Not found", status_code=404)(
                scope, receive, send
            )
        if (is_admin or callback) and project is None:
            response = (
                RedirectResponse("/projects", status_code=303)
                if path == "/admin"
                else JSONResponse({"detail": "Проект не найден"}, status_code=404)
            )
            return await response(scope, receive, send)
        state = scope.setdefault("state", {})
        state["project"] = projects.public_project(project) if project else None
        state["project_prefix"] = prefix
        state["projects"] = (
            [projects.public_project(p) for p in projects.list_projects()]
            if scope.get("session", {}).get("admin_authenticated")
            else []
        )

        async def scoped_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if prefix:

                    def scoped_location(value):
                        # Starlette's automatic slash redirect is absolute.
                        # It must not send project B to legacy /admin in A.
                        target = urlsplit(value.decode("latin-1"))
                        origin = Request(scope).url.netloc
                        if target.netloc and target.netloc != origin:
                            return value
                        if target.path == "/admin" or target.path.startswith("/admin/"):
                            return urlunsplit(
                                target._replace(path=prefix + target.path)
                            ).encode("latin-1")
                        return value

                    headers = [
                        (
                            k,
                            scoped_location(v) if k == b"location" else v,
                        )
                        for k, v in headers
                    ]
                if is_admin or path.startswith("/projects"):
                    headers.append((b"cache-control", b"private, no-store"))
                message = dict(message, headers=headers)
            await send(message)

        with projects.project_scope(project), defer_outgoing_audits():
            await self.app(scope, receive, scoped_send)


def project_page(request, error=None, notice=None, code=200):
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    return templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "section": "projects",
            "csrf": request.session["csrf"],
            "projects": [projects.public_project(p) for p in projects.list_projects()],
            "deleted_projects": [
                projects.public_project(p)
                for p in projects.list_projects(include_deleted=True)
                if p.is_deleted
            ],
            "error": error,
            "notice": notice,
            "callback_base_url": (
                get_settings().public_base_url or str(request.base_url).rstrip("/")
            )
            + "/",
        },
        status_code=code,
    )


@router.get("/projects", response_class=HTMLResponse)
async def overview(request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    return project_page(request)


@router.post("/projects", dependencies=[Depends(protect_admin_form)])
async def create_project(request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        entered_group = str(form.get("group_id", "")).strip()
        if (
            not entered_group.isascii()
            or not entered_group.isdigit()
            or len(entered_group) > 10
        ):
            raise ValueError(
                "ID сообщества — положительное число без минуса, например 240572018."
            )
        project = projects.create_project(
            name=str(form.get("name", "")),
            group_id=int(entered_group),
            token=str(form.get("token", "")),
            callback_secret=str(form.get("callback_secret", "")),
            confirmation_code=str(form.get("confirmation_code", "")),
            enabled=False,
        )
    except ValueError as error:
        return project_page(request, error=str(error), code=422)
    return RedirectResponse(f"/projects#project-{project.id}", status_code=303)


@router.post(
    "/projects/{project_id}/settings", dependencies=[Depends(protect_admin_form)]
)
async def update_project(project_id: int, request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        projects.update_project(
            project_id,
            name=str(form.get("name", "")),
            token=str(form.get("token", "")).strip() or None,
            callback_secret=str(form.get("callback_secret", "")).strip() or None,
            confirmation_code=str(form.get("confirmation_code", "")).strip() or None,
            video_token=(
                ""
                if form.get("clear_video_token")
                else str(form.get("video_token", "")).strip() or None
            ),
            enabled=bool(form.get("enabled")),
        )
    except ValueError as error:
        return project_page(request, error=str(error), code=422)
    return RedirectResponse(f"/projects#project-{project_id}", status_code=303)


@router.post("/projects/{project_id}/check", dependencies=[Depends(protect_admin_form)])
async def check_project(project_id: int, request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    try:
        project = projects.get_project(project_id)
    except ValueError:
        raise HTTPException(404, "Проект не найден")
    if project is None:
        raise HTTPException(404, "Проект не найден")
    with projects.project_scope(project):
        settings = get_settings()
        if not settings.vk_group_token:
            return project_page(
                request, error="Сначала сохраните токен сообщества.", code=422
            )
        try:
            # No group_id: VK returns the community which OWNS this token, not
            # arbitrary public information about a user-supplied group ID.
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.vk.com/method/groups.getById",
                    data={
                        "access_token": settings.vk_group_token,
                        "v": settings.vk_api_version,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            result = payload.get("response", {})
            groups = result.get("groups", []) if isinstance(result, dict) else result
            group = (
                next(
                    (
                        g
                        for g in groups
                        if isinstance(g, dict) and g.get("id") == project.group_id
                    ),
                    None,
                )
                if isinstance(groups, list)
                else None
            )
            if not group:
                return project_page(
                    request,
                    error="VK не подтвердил принадлежность токена этой группе. Проверьте ID и ключ.",
                    code=422,
                )
        except (httpx.HTTPError, ValueError):
            return project_page(
                request,
                error="Не удалось проверить подключение к VK. Повторите позже.",
                code=422,
            )
    form = await request.form()
    refresh_name = form.get("refresh_name") == "1"
    # Custom names are preserved by ordinary connection checks. Explicit name
    # refresh deliberately replaces them, only after verifying token ownership.
    group_name = str(group.get("name") or "").strip()
    renamed = False
    if group_name and (refresh_name or project.name == f"VK {project.group_id}"):
        current = projects.get_project(project.id)
        if (
            current
            and current.token == project.token
            and (refresh_name or current.name == project.name)
        ):
            projects.update_project(project.id, name=group_name[:120])
            renamed = True
    return project_page(
        request,
        notice=(f"Название обновлено: {group_name[:120]}. " if renamed else "")
        + "Токен относится к указанной группе. Это не проверка прав отправки и настройки Callback API."
        + (" VK не вернул название группы." if refresh_name and not group_name else ""),
    )


@router.post(
    "/projects/{project_id}/delete", dependencies=[Depends(protect_admin_form)]
)
async def delete_project(project_id: int, request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        projects.delete_project(project_id, str(form.get("confirmation", "")))
    except ValueError as error:
        return project_page(request, error=str(error), code=422)
    return RedirectResponse("/projects#project-trash", status_code=303)


@router.post(
    "/projects/{project_id}/restore", dependencies=[Depends(protect_admin_form)]
)
async def restore_project(project_id: int, request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse("/login", status_code=303)
    try:
        projects.restore_project(project_id)
    except ValueError as error:
        return project_page(request, error=str(error), code=422)
    return RedirectResponse(f"/projects#project-{project_id}", status_code=303)
