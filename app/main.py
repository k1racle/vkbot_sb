import logging
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func

from .config import get_settings
from .db import (
    ProcessedComment,
    SessionLocal,
    already_processed,
    already_sent_to_user,
    init_db,
    read_settings,
    save_settings,
)
from .vk_api import VkApiError, is_group_member, send_message, upload_file_for_message

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)
app = FastAPI(title="VK Comment Promo Bot")
templates = Jinja2Templates(directory="app/templates")
app.add_middleware(SessionMiddleware, secret_key=settings.admin_session_secret, https_only=False, max_age=60 * 60 * 12)
ATTACHMENT_DIR = Path("data")
ATTACHMENT_DIR.mkdir(exist_ok=True)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def admin_required(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))


def setting(values: dict[str, str], name: str, default):
    value = values.get(name)
    return default if value is None else value


def as_bool(value: str | bool) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if admin_required(request):
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == settings.admin_username and password == settings.admin_password:
        request.session["admin_authenticated"] = True
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        values = read_settings(session)
        total = session.query(func.count(ProcessedComment.id)).scalar() or 0
        sent = session.query(func.count(ProcessedComment.id)).filter_by(status="sent").scalar() or 0
        failed = session.query(func.count(ProcessedComment.id)).filter_by(status="failed").scalar() or 0
        recent = session.query(ProcessedComment).order_by(ProcessedComment.id.desc()).limit(30).all()
    form = {
        "promo_code": values.get("promo_code", settings.promo_code),
        "shop_url": values.get("shop_url", settings.shop_url),
        "promo_message": values.get("promo_message", settings.promo_message).replace("\\n", "\n"),
        "promo_attachments": values.get("promo_attachments", settings.promo_attachments),
        "attachment_name": values.get("attachment_name", ""),
        "allowed_post_ids": values.get("allowed_post_ids", settings.allowed_post_ids),
        "stop_words": values.get("stop_words", settings.stop_words),
        "min_comment_length": values.get("min_comment_length", str(settings.min_comment_length)),
        "one_promo_per_user": as_bool(values.get("one_promo_per_user", str(settings.one_promo_per_user))),
        "test_mode": as_bool(values.get("test_mode", str(settings.test_mode))),
        "test_trigger_phrase": values.get("test_trigger_phrase", settings.test_trigger_phrase),
    }
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "form": form, "stats": {"total": total, "sent": sent, "failed": failed}, "recent": recent},
    )


@app.post("/admin/settings")
async def update_admin_settings(
    request: Request,
    promo_code: str = Form(...),
    shop_url: str = Form(...),
    promo_message: str = Form(...),
    promo_attachments: str = Form(""),
    allowed_post_ids: str = Form(""),
    stop_words: str = Form(""),
    min_comment_length: int = Form(1),
    one_promo_per_user: str | None = Form(None),
    test_mode: str | None = Form(None),
    test_trigger_phrase: str = Form("тестовое сообщение"),
    attachment: UploadFile | None = File(None),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    values_to_save = {
        "promo_code": promo_code.strip(),
        "shop_url": shop_url.strip(),
        "promo_message": promo_message,
        "promo_attachments": promo_attachments.strip(),
        "allowed_post_ids": allowed_post_ids.strip(),
        "stop_words": stop_words.strip(),
        "min_comment_length": str(max(0, min_comment_length)),
        "one_promo_per_user": "true" if one_promo_per_user else "false",
        "test_mode": "true" if test_mode else "false",
        "test_trigger_phrase": test_trigger_phrase.strip() or "тестовое сообщение",
    }
    if attachment and attachment.filename:
        if not attachment.content_type:
            return RedirectResponse("/admin?attachment_error=empty", status_code=status.HTTP_303_SEE_OTHER)
        allowed = attachment.content_type.startswith(("image/", "video/", "audio/")) or attachment.content_type in {
            "application/pdf", "application/zip", "application/x-zip-compressed", "text/plain"
        }
        if not allowed:
            return RedirectResponse("/admin?attachment_error=type", status_code=status.HTTP_303_SEE_OTHER)
        data = await attachment.read()
        if len(data) > 50 * 1024 * 1024:
            return RedirectResponse("/admin?attachment_error=size", status_code=status.HTTP_303_SEE_OTHER)
        path = ATTACHMENT_DIR / f"promo_{uuid4().hex}"
        path.write_bytes(data)
        with SessionLocal() as session:
            old_path = read_settings(session).get("attachment_path")
            values_to_save.update({
                "attachment_path": str(path),
                "attachment_name": attachment.filename,
                "attachment_type": attachment.content_type,
            })
            save_settings(session, values_to_save)
        if old_path:
            Path(old_path).unlink(missing_ok=True)
    else:
        with SessionLocal() as session:
            save_settings(session, values_to_save)
    return RedirectResponse("/admin?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/attachment")
async def upload_admin_attachment(request: Request, attachment: UploadFile = File(...)):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if not attachment.filename or not attachment.content_type:
        return RedirectResponse("/admin?attachment_error=empty", status_code=status.HTTP_303_SEE_OTHER)
    allowed = attachment.content_type.startswith(("image/", "video/", "audio/")) or attachment.content_type in {
        "application/pdf",
        "application/zip",
        "application/x-zip-compressed",
        "text/plain",
    }
    if not allowed:
        return RedirectResponse("/admin?attachment_error=type", status_code=status.HTTP_303_SEE_OTHER)

    data = await attachment.read()
    if len(data) > 50 * 1024 * 1024:
        return RedirectResponse("/admin?attachment_error=size", status_code=status.HTTP_303_SEE_OTHER)

    path = ATTACHMENT_DIR / f"promo_{uuid4().hex}"
    path.write_bytes(data)
    with SessionLocal() as session:
        old_path = read_settings(session).get("attachment_path")
        save_settings(
            session,
            {
                "attachment_path": str(path),
                "attachment_name": attachment.filename,
                "attachment_type": attachment.content_type,
            },
        )
    if old_path:
        Path(old_path).unlink(missing_ok=True)
    return RedirectResponse("/admin?attachment_saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.api_route("/admin/attachment/delete", methods=["GET", "POST"])
async def delete_admin_attachment(request: Request):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        values = read_settings(session)
        save_settings(session, {"attachment_path": "", "attachment_name": "", "attachment_type": ""})
    if values.get("attachment_path"):
        Path(values["attachment_path"]).unlink(missing_ok=True)
    return RedirectResponse("/admin?attachment_deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/vk/callback", response_class=PlainTextResponse)
async def vk_callback(request: Request) -> str:
    payload = await request.json()

    if payload.get("secret") != settings.vk_callback_secret:
        logger.warning("Rejected callback with invalid secret")
        return "invalid secret"

    if payload.get("type") == "confirmation":
        return settings.vk_confirmation_code
    if payload.get("type") != "wall_reply_new":
        return "ok"

    obj = payload.get("object") or {}
    comment_id = int(obj.get("id", 0))
    post_id = int(obj.get("post_id", 0))
    user_id = int(obj.get("from_id", 0))
    comment_text = str(obj.get("text", "")).strip()
    if not comment_id or user_id <= 0:
        return "ok"
    with SessionLocal() as session:
        values = read_settings(session)
        if already_processed(session, comment_id):
            return "ok"
        session.add(ProcessedComment(comment_id=comment_id, post_id=post_id, user_id=user_id))
        session.commit()

    try:
        test_enabled = as_bool(setting(values, "test_mode", settings.test_mode))
        test_phrase = str(setting(values, "test_trigger_phrase", settings.test_trigger_phrase)).strip().casefold()
        if test_enabled and test_phrase not in comment_text.casefold():
            update_status(comment_id, "test_filtered")
            return "ok"

        min_length = int(setting(values, "min_comment_length", settings.min_comment_length) or 1)
        if len(comment_text) < min_length:
            update_status(comment_id, "too_short")
            return "ok"

        stop_words = [
            word.strip().casefold()
            for word in str(setting(values, "stop_words", settings.stop_words)).replace(",", "\n").splitlines()
            if word.strip()
        ]
        if any(word in comment_text.casefold() for word in stop_words):
            update_status(comment_id, "stop_word")
            return "ok"

        if as_bool(setting(values, "one_promo_per_user", settings.one_promo_per_user)):
            with SessionLocal() as session:
                if already_sent_to_user(session, user_id):
                    update_status(comment_id, "already_sent")
                    return "ok"

        post_ids = values.get("allowed_post_ids", settings.allowed_post_ids).strip()
        if post_ids and post_id not in {int(value.strip()) for value in post_ids.split(",") if value.strip()}:
            update_status(comment_id, "post_filtered")
            return "ok"
        if not await is_group_member(user_id):
            update_status(comment_id, "not_member")
            return "ok"

        template = values.get("promo_message", settings.promo_message).replace("\\n", "\n")
        text = template.format(
            promo_code=values.get("promo_code", settings.promo_code),
            shop_url=values.get("shop_url", settings.shop_url),
        )
        attachments = values.get("promo_attachments", settings.promo_attachments)
        attachment_path = values.get("attachment_path", "")
        if attachment_path and Path(attachment_path).exists():
            attachments = await upload_file_for_message(
                user_id,
                Path(attachment_path),
                values.get("attachment_name", "attachment"),
                values.get("attachment_type", "application/octet-stream"),
            )
        await send_message(user_id, text, random_id=comment_id, attachment=attachments)
        update_status(comment_id, "sent")
        logger.info("Promo sent: comment=%s user=%s", comment_id, user_id)
    except VkApiError as error:
        update_status(comment_id, "failed", str(error))
        logger.warning("VK rejected message for user %s: %s", user_id, error)
    except Exception as error:
        update_status(comment_id, "failed", str(error))
        logger.exception("Failed to process comment %s", comment_id)

    return "ok"


def update_status(comment_id: int, status: str, error: str | None = None) -> None:
    with SessionLocal() as session:
        record = session.query(ProcessedComment).filter_by(comment_id=comment_id).first()
        if record:
            record.status = status
            record.error = error
            session.commit()
