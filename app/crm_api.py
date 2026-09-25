import secrets
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import broadcasts, clients, db, vk_api
from .config import get_settings
from .operators import configured_operators
from .scenario_api import authorize

router = APIRouter(prefix="/admin/api", dependencies=[Depends(authorize)])


class BroadcastInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    media_id: str = Field(default="", max_length=32)
    audience: str = "all"
    user_ids: list[int] = Field(default_factory=list, max_length=10000)


class StartInput(BaseModel):
    confirm_consent: bool = False
    expected_count: int = Field(ge=1)


class TestInput(BaseModel):
    user_id: int = Field(gt=0, lt=2_000_000_000)


def get_job(session, ident):
    job = session.query(db.Broadcast).filter_by(id=ident).with_for_update().first()
    if not job:
        raise HTTPException(404, "Рассылка не найдена")
    return job


@router.get("/clients")
def client_list(
    q: str = Query("", max_length=120),
    page: int = Query(1, ge=1),
    contacted: bool = False,
):
    with db.SessionLocal() as session:
        query = clients.list_query(session, q, contacted)
        total = query.count()
        rows = (
            query.order_by(db.Client.user_id.desc())
            .offset((page - 1) * 30)
            .limit(30)
            .all()
        )
        return {
            "items": [clients.serialize(c) for c in rows],
            "total": total,
            "page": page,
            "pages": max(1, (total + 29) // 30),
            "profiles_pending": session.query(db.Client)
            .filter_by(profile_requested=True)
            .count(),
        }


@router.post("/clients/refresh")
def refresh_clients():
    clients.backfill_clients()
    with db.SessionLocal() as session:
        count = session.query(db.Client).update({"profile_requested": True})
        session.commit()
        return {"queued": count}


@router.get("/clients/{user_id}")
def client_detail(user_id: int):
    with db.SessionLocal() as session:
        client = session.get(db.Client, user_id)
        if not client:
            raise HTTPException(404, "Клиент не найден")
        row = session.get(db.Conversation, user_id)
        return {
            "client": clients.serialize(client),
            "handoff": row.handoff if row else False,
            "assigned_operator_id": row.assigned_operator_id if row else None,
            "variables": {
                k: v for k, v in row.variables.items() if not k.startswith("_")
            }
            if row
            else {},
            "events": [
                {
                    "text": e.text,
                    "status": e.status,
                    "kind": e.kind,
                    "date": str(e.created_at),
                    "error": e.error,
                }
                for e in session.query(db.DialogEvent)
                .filter_by(user_id=user_id)
                .order_by(db.DialogEvent.id.desc())
                .limit(30)
            ],
            "deliveries": [
                {
                    "status": r.status,
                    "error": r.error,
                    "broadcast_id": r.broadcast_id,
                    "date": str(r.sent_at) if r.sent_at else None,
                }
                for r in session.query(db.BroadcastRecipient)
                .filter_by(user_id=user_id)
                .order_by(db.BroadcastRecipient.id.desc())
                .limit(20)
            ],
        }


@router.post("/clients/{user_id}/unsubscribe")
def unsubscribe_client(user_id: int):
    with db.SessionLocal() as session:
        client = session.get(db.Client, user_id)
        if not client:
            raise HTTPException(404, "Клиент не найден")
        client.unsubscribed = True
        session.commit()
    return {"ok": True}


@router.get("/broadcasts")
def list_broadcasts():
    with db.SessionLocal() as session:
        return {
            "items": [
                broadcasts.serialize(session, row)
                for row in session.query(db.Broadcast)
                .order_by(db.Broadcast.created_at.desc())
                .limit(50)
            ],
            "eligible": clients.eligible_query(
                session, configured_operators(db.read_settings(session), get_settings())
            ).count(),
        }


@router.post("/broadcasts")
def prepare_broadcast(body: BroadcastInput):
    if (
        body.audience not in {"all", "selected"}
        or not body.title.strip()
        or not body.message.strip()
    ):
        raise HTTPException(422, "Заполните название, текст и выберите получателей")
    with db.SessionLocal() as session:
        if body.media_id:
            asset = session.get(db.MediaAsset, body.media_id)
            if not asset or not Path(asset.path).is_file():
                raise HTTPException(422, "Вложение не найдено. Загрузите файл заново")
        query = clients.eligible_query(
            session, configured_operators(db.read_settings(session), get_settings())
        )
        if body.audience == "selected":
            query = query.filter(db.Client.user_id.in_(set(body.user_ids)))
        recipients = query.order_by(db.Client.user_id).all()
        if not recipients:
            raise HTTPException(
                422,
                "Нет подходящих получателей: нужны успешные исходящие сообщения бота; отписавшиеся и менеджеры исключаются",
            )
        if len(recipients) > 10000:
            raise HTTPException(
                422, "В одной рассылке можно выбрать не более 10 000 клиентов"
            )
        job = db.Broadcast(
            id=uuid4().hex,
            title=body.title.strip(),
            message=body.message,
            media_id=body.media_id,
            status="draft",
        )
        session.add(job)
        session.add_all(
            [
                db.BroadcastRecipient(broadcast_id=job.id, user_id=c.user_id)
                for c in recipients
            ]
        )
        session.commit()
        result = broadcasts.serialize(session, job)
        result["sample"] = [clients.serialize(c) for c in recipients[:5]]
        result["preview"] = broadcasts.content(job.message, recipients[0])
        return result  # Draft creation never sends anything.


@router.get("/broadcasts/{ident}")
def broadcast_detail(ident: str, page: int = Query(1, ge=1)):
    with db.SessionLocal() as session:
        job = session.get(db.Broadcast, ident)
        if not job:
            raise HTTPException(404, "Рассылка не найдена")
        result = broadcasts.serialize(session, job)
        result["recipients"] = [
            {
                "user_id": row.user_id,
                "status": row.status,
                "attempts": row.attempts,
                "error": row.error,
            }
            for row in session.query(db.BroadcastRecipient)
            .filter_by(broadcast_id=ident)
            .order_by(db.BroadcastRecipient.id)
            .offset((page - 1) * 50)
            .limit(50)
        ]
        result["page"] = page
        return result


@router.post("/broadcasts/{ident}/start")
def start_broadcast(ident: str, body: StartInput):
    if not body.confirm_consent:
        raise HTTPException(
            422, "Подтвердите наличие согласия получателей на эту рассылку"
        )
    with db.SessionLocal() as session:
        job = get_job(session, ident)
        if job.status != "draft":
            raise HTTPException(
                409, "Рассылка уже запущена или отменена. Обновите список"
            )
        total = (
            session.query(db.BroadcastRecipient).filter_by(broadcast_id=ident).count()
        )
        if total != body.expected_count:
            raise HTTPException(
                409, "Состав получателей изменился. Откройте предпросмотр заново"
            )
        job.status, job.consent_confirmed, job.started_at = (
            "queued",
            True,
            clients.now(),
        )
        session.commit()
        return broadcasts.serialize(session, job)


@router.post("/broadcasts/{ident}/test")
async def test_broadcast(ident: str, body: TestInput):
    with db.SessionLocal() as session:
        job = session.get(db.Broadcast, ident)
        if not job:
            raise HTTPException(404, "Рассылка не найдена")
        client = session.get(db.Client, body.user_id) or db.Client(
            first_name="друг", last_name=""
        )
        if client.unsubscribed:
            raise HTTPException(
                422,
                "Этот получатель отписался от рассылок. Выберите другого тестового пользователя",
            )
        message = broadcasts.content(job.message, client)
        asset = session.get(db.MediaAsset, job.media_id) if job.media_id else None
        if job.media_id and (not asset or not Path(asset.path).is_file()):
            raise HTTPException(
                422, "Вложение недоступно. Загрузите файл в новую рассылку"
            )
    try:
        if not await vk_api.is_messages_allowed(body.user_id):
            raise HTTPException(422, "Получатель должен разрешить сообщения сообщества")
        attachment = (
            await vk_api.upload_file_for_message(
                body.user_id, Path(asset.path), asset.filename, asset.content_type
            )
            if asset
            else ""
        )
        await vk_api.send_message(
            body.user_id,
            "[Тест рассылки]\n" + message,
            random_id=secrets.randbelow(2147483646) + 1,
            attachment=attachment,
            keyboard=broadcasts.keyboard(),
        )
    except vk_api.VkApiError as error:
        raise HTTPException(422, f"VK не принял тест: {error}") from error
    except (httpx.HTTPError, OSError) as error:
        raise HTTPException(
            422, "Не удалось отправить тест. Проверьте соединение и файл"
        ) from error
    return {"ok": True}


@router.post("/broadcasts/{ident}/{action}")
def control_broadcast(ident: str, action: str):
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(404)
    with db.SessionLocal() as session:
        job = get_job(session, ident)
        if action == "pause" and job.status in {"queued", "running"}:
            job.status = "paused"
        elif action == "resume" and job.status == "paused" and job.consent_confirmed:
            job.status, job.error = "queued", ""
        elif action == "cancel" and job.status in {
            "draft",
            "queued",
            "running",
            "paused",
        }:
            job.status = "cancelled"
            session.query(db.BroadcastRecipient).filter_by(
                broadcast_id=ident, status="pending"
            ).update({"status": "cancelled"})
        else:
            raise HTTPException(
                409, "Действие недоступно для текущего состояния рассылки"
            )
        session.commit()
        return broadcasts.serialize(session, job)
