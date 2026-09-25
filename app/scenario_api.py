import copy
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from .db import (
    Campaign,
    Conversation,
    DialogEvent,
    MediaAsset,
    Scenario,
    SessionLocal,
)
from .flows import Graph, advance, render, starter_graph, validate_graph


def authorize(request: Request):
    if not request.session.get("admin_authenticated"):
        raise HTTPException(401, "Войдите в панель управления")
    if request.method not in {"GET", "HEAD"}:
        expected = request.session.get("csrf", "")
        if not expected or not secrets.compare_digest(
            request.headers.get("X-CSRF-Token", ""), expected
        ):
            raise HTTPException(403, "Обновите страницу и повторите действие")


router = APIRouter(prefix="/admin/api", dependencies=[Depends(authorize)])


class DraftInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    graph: Graph
    revision: int = Field(ge=0)


class RevisionInput(BaseModel):
    revision: int = Field(ge=0)


class PreviewInput(BaseModel):
    graph: Graph
    state: dict = Field(default_factory=dict)
    text: str = Field(default="", max_length=4000)
    payload: dict = Field(default_factory=dict)
    member: bool = True
    restart: bool = False


def serialize(item):
    return {
        "id": item.id,
        "title": item.title,
        "graph": item.draft,
        "revision": item.revision,
        "version": item.version,
        "active": item.active,
        "has_published": item.published is not None,
        "has_changes": item.draft != item.published,
    }


def checked(session, graph):
    return validate_graph(
        graph,
        [c.id for c in session.query(Campaign).filter_by(enabled=True)],
        [a.id for a in session.query(MediaAsset)],
    )


def get_scenario(session, scenario_id, revision=None):
    item = session.query(Scenario).filter_by(id=scenario_id).with_for_update().first()
    if item is None:
        raise HTTPException(404, "Сценарий не найден")
    if revision is not None and item.revision != revision:
        raise HTTPException(
            409,
            "Сценарий изменён в другой вкладке. Обновите страницу перед сохранением.",
        )
    return item


@router.get("/scenarios")
def list_scenarios():
    with SessionLocal() as session:
        return {
            "scenarios": [
                serialize(s) for s in session.query(Scenario).order_by(Scenario.id)
            ],
            "campaigns": [
                {"id": c.id, "title": c.title, "enabled": c.enabled}
                for c in session.query(Campaign)
            ],
            "media": [
                {"id": a.id, "filename": a.filename} for a in session.query(MediaAsset)
            ],
        }


@router.post("/scenarios")
def create_scenario():
    with SessionLocal() as session:
        item = Scenario(title="Диалог с клиентом", draft=starter_graph())
        session.add(item)
        session.commit()
        return serialize(item)


@router.put("/scenarios/{scenario_id}")
def save_scenario(scenario_id: int, body: DraftInput):
    with SessionLocal() as session:
        item = get_scenario(session, scenario_id, body.revision)
        item.title, item.draft = (
            body.title.strip() or "Новый сценарий",
            body.graph.model_dump(),
        )
        item.revision += 1
        session.commit()
        return serialize(item)


@router.post("/scenarios/{scenario_id}/publish")
def publish_scenario(scenario_id: int, body: RevisionInput):
    with SessionLocal() as session:
        # One published entry scenario for incoming private messages.
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(-731942)"))
        item = get_scenario(session, scenario_id, body.revision)
        errors = checked(session, item.draft)
        if errors:
            raise HTTPException(422, errors)
        session.query(Scenario).filter(Scenario.id != item.id).update({"active": False})
        item.published, item.active = copy.deepcopy(item.draft), True
        item.version += 1
        item.revision += 1
        session.commit()
        return serialize(item)


@router.post("/scenarios/{scenario_id}/pause")
def pause_scenario(scenario_id: int, body: RevisionInput):
    with SessionLocal() as session:
        item = get_scenario(session, scenario_id, body.revision)
        item.active = False
        item.revision += 1
        session.commit()
        return serialize(item)


@router.post("/scenarios/validate")
def validate_scenario(graph: Graph):
    with SessionLocal() as session:
        return {"errors": checked(session, graph.model_dump())}


@router.post("/media")
async def upload_media(file: UploadFile = File(...)):
    mime = file.content_type or ""
    if not (
        mime.startswith(("image/", "video/", "audio/"))
        or mime
        in {
            "application/pdf",
            "application/zip",
            "application/x-zip-compressed",
            "text/plain",
        }
    ):
        raise HTTPException(
            422, "Выберите изображение, видео, аудио, PDF, ZIP или текстовый файл"
        )
    data = await file.read(50 * 1024 * 1024 + 1)
    if not data or len(data) > 50 * 1024 * 1024:
        raise HTTPException(422, "Файл должен быть непустым и не больше 50 МБ")
    asset_id = secrets.token_hex(16)
    path = Path("data") / f"flow_{asset_id}"
    path.write_bytes(data)
    with SessionLocal() as session:
        asset = MediaAsset(
            id=asset_id,
            filename=(file.filename or "file")[:255],
            content_type=mime,
            path=str(path),
        )
        session.add(asset)
        session.commit()
        return {"id": asset.id, "filename": asset.filename}


@router.get("/media/{asset_id}")
def read_media(asset_id: str):
    with SessionLocal() as session:
        asset = session.get(MediaAsset, asset_id)
        if not asset or not Path(asset.path).is_file():
            raise HTTPException(404, "Файл не найден")
        return FileResponse(
            asset.path,
            filename=asset.filename,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )


@router.post("/preview")
async def preview(body: PreviewInput):
    graph = body.graph.model_dump()
    with SessionLocal() as session:
        errors = checked(session, graph)
        if errors:
            raise HTTPException(422, errors)
        state = (
            copy.deepcopy(body.state)
            if body.state
            else {
                "version": 0,
                "variables": {"first_name": "Анна", "user_name": "Анна"},
            }
        )
        state.setdefault("version", 0)
        state.setdefault("variables", {})
        messages = []

        class PreviewPort:
            def nonce(self, step):
                return secrets.token_hex(8)

            async def emit(self, message, keyboard=None, media_id="", note=""):
                asset = session.get(MediaAsset, media_id) if media_id else None
                messages.append(
                    {
                        "text": message,
                        "keyboard": keyboard
                        if keyboard is not None
                        else {"one_time": False, "buttons": []},
                        "file": asset.filename if asset else "",
                        "note": note,
                    }
                )

            async def check(self, node):
                if node["condition"] == "member":
                    return body.member
                return node["campaign_id"] in state.get("promos", [])

            async def promo(self, campaign_id, variables):
                campaign = session.get(Campaign, campaign_id)
                if not body.member:
                    await self.emit(
                        "Подпишитесь на сообщество, чтобы получить промокод. Затем попробуйте снова через меню."
                    )
                elif campaign.one_promo_per_user and campaign_id in state.get(
                    "promos", []
                ):
                    await self.emit(
                        "Вы уже получали промокод этой акции. Он есть выше в переписке."
                    )
                else:
                    variables.update(
                        promo_code=campaign.promo_code, shop_url=campaign.shop_url
                    )
                    await self.emit(
                        render(campaign.promo_message, variables),
                        note=campaign.attachment_name or "",
                    )
                    state.setdefault("promos", []).append(campaign_id)

            async def handoff(self, message):
                await self.emit(
                    message or "Передаю диалог менеджеру.",
                    note="В реальном диалоге автоответы приостановятся. Менеджер отвечает из сообщений сообщества.",
                )

        await advance(
            graph,
            state,
            body.text,
            body.payload,
            PreviewPort(),
            restart=body.restart
            or body.text.strip().casefold() in {"меню", "начать", "старт", "/start"},
        )
        return {"state": state, "messages": messages}


@router.get("/conversations")
def conversations():
    with SessionLocal() as session:
        rows = (
            session.query(Conversation)
            .order_by(Conversation.updated_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "user_id": c.user_id,
                "name": c.variables.get("first_name", f"id{c.user_id}"),
                "handoff": c.handoff,
                "node_id": c.node_id,
                "variables": {
                    k: v for k, v in c.variables.items() if not k.startswith("_")
                },
                "events": [
                    {
                        "text": e.text,
                        "status": e.status,
                        "error": e.error,
                        "date": str(e.created_at),
                    }
                    for e in session.query(DialogEvent)
                    .filter_by(user_id=c.user_id)
                    .order_by(DialogEvent.id.desc())
                    .limit(15)
                ],
            }
            for c in rows
        ]


@router.post("/conversations/{user_id}/resume")
def resume(user_id: int):
    with SessionLocal() as session:
        if session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_id}
            )
        row = session.get(Conversation, user_id)
        if not row:
            raise HTTPException(404, "Диалог не найден")
        row.handoff, row.node_id = False, ""
        session.commit()
        return {"ok": True}
