"""Project-local recycle bin. Keep identifiers, files, opt-outs and audit history."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import db
from .scenario_api import authorize

router = APIRouter(prefix="/admin/api/trash", dependencies=[Depends(authorize)])
MODELS = {"campaign": db.Campaign, "scenario": db.Scenario, "broadcast": db.Broadcast}


def scenario_lock(session):
    # Same lock as publication: delete/restore cannot race an activation.
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(-731942)"))


def remove_campaign(session, item):
    if item.is_deleted:
        return
    item.archived_post_id = item.post_id
    # Negative IDs are not accepted by campaign forms or callback normalization.
    # Retain the campaign ID for history while freeing its UNIQUE post binding.
    item.post_id = -item.id
    item.is_deleted, item.enabled = True, False
    session.query(db.PendingGift).filter_by(
        campaign_id=item.id, status="pending"
    ).update(
        {"status": "cancelled", "active_key": None, "awaiting_subscription": False}
    )


def remove_broadcast(session, item):
    if item.is_deleted:
        return
    item.is_deleted = True
    if item.status not in {"completed", "cancelled"}:
        item.status = "cancelled"
    session.query(db.BroadcastRecipient).filter_by(
        broadcast_id=item.id, status="pending"
    ).update({"status": "cancelled"})
    # Do not forge results for sends already in flight; their audit may finish.


@router.get("")
def contents():
    with db.SessionLocal() as session:
        result = []
        for kind, model in MODELS.items():
            for item in (
                session.query(model)
                .filter_by(is_deleted=True)
                .order_by(model.id.desc())
            ):
                result.append({"kind": kind, "id": item.id, "title": item.title})
        return {"items": result}


@router.post("/{kind}/{ident}/restore")
def restore(kind: str, ident: str):
    model = MODELS.get(kind)
    if model is None:
        raise HTTPException(404, "Неизвестный тип объекта")
    if kind != "broadcast":
        if (
            not ident.isascii()
            or not ident.isdigit()
            or not 0 < int(ident) <= 2147483647
        ):
            raise HTTPException(404, "Объект не найден")
        ident = int(ident)
    with db.SessionLocal() as session:
        if kind == "scenario":
            scenario_lock(session)
        item = (
            session.query(model)
            .filter_by(id=ident, is_deleted=True)
            .with_for_update()
            .first()
        )
        if item is None:
            raise HTTPException(404, "Объект не найден в корзине этого проекта")
        if kind == "campaign":
            conflict = (
                session.query(db.Campaign)
                .filter_by(post_id=item.archived_post_id)
                .first()
            )
            if conflict:
                raise HTTPException(
                    409,
                    "Для этой публикации уже есть кампания. Сначала удалите или перенастройте её.",
                )
            item.post_id, item.archived_post_id = item.archived_post_id, None
            item.enabled = False
        elif kind == "scenario":
            item.active = False
            item.revision += 1
        # Restoring a broadcast never re-queues it, including a former draft.
        item.is_deleted = False
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                409, "Объект конфликтует с уже существующим. Обновите страницу."
            ) from None
    return {"ok": True}
