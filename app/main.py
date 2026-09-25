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
    Campaign,
    ProcessedComment,
    SessionLocal,
    already_processed,
    already_sent_to_user,
    init_db,
    read_settings,
    save_settings,
)
from .vk_api import VkApiError, get_user_name, is_group_member, send_message, upload_file_for_message

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)
app = FastAPI(title="VK Бот")
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
async def admin_page(request: Request, campaign_id: int | None = None, new_campaign: bool = False):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        values = read_settings(session)
        total = session.query(func.count(ProcessedComment.id)).scalar() or 0
        sent = session.query(func.count(ProcessedComment.id)).filter_by(status="sent").scalar() or 0
        failed = session.query(func.count(ProcessedComment.id)).filter_by(status="failed").scalar() or 0
        status_counts = dict(
            session.query(ProcessedComment.status, func.count(ProcessedComment.id))
            .group_by(ProcessedComment.status)
            .all()
        )
        recent = session.query(ProcessedComment).order_by(ProcessedComment.id.desc()).limit(30).all()
        campaigns = session.query(Campaign).order_by(Campaign.post_id.desc()).all()
        selected_campaign = None if new_campaign else (session.get(Campaign, campaign_id) if campaign_id else (campaigns[0] if campaigns else None))
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
        "admin_test_user_id": values.get("admin_test_user_id", settings.admin_test_user_id),
        "chat_enabled": as_bool(values.get("chat_enabled", settings.chat_enabled)),
        "chat_greeting": values.get("chat_greeting", settings.chat_greeting),
        "operator_user_id": values.get("operator_user_id", settings.operator_user_id),
        "operator_trigger_words": values.get("operator_trigger_words", settings.operator_trigger_words),
        "operator_ack": values.get("operator_ack", settings.operator_ack),
    }
    campaign_form = {
        "id": selected_campaign.id if selected_campaign else "",
        "post_id": selected_campaign.post_id if selected_campaign else "",
        "title": selected_campaign.title if selected_campaign else "",
        "promo_code": selected_campaign.promo_code if selected_campaign else "",
        "shop_url": selected_campaign.shop_url if selected_campaign else "",
        "promo_message": selected_campaign.promo_message if selected_campaign else "",
        "enabled": selected_campaign.enabled if selected_campaign else True,
        "attachment_name": selected_campaign.attachment_name if selected_campaign else "",
        "stop_words": selected_campaign.stop_words if selected_campaign else "",
        "min_comment_length": selected_campaign.min_comment_length if selected_campaign else 1,
        "one_promo_per_user": selected_campaign.one_promo_per_user if selected_campaign else False,
    }
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "form": form, "campaign_form": campaign_form, "stats": {"total": total, "sent": sent, "failed": failed, "status_counts": status_counts}, "recent": recent, "campaigns": campaigns},
    )


@app.post("/admin/settings")
async def update_admin_settings(
    request: Request,
    test_mode: str | None = Form(None),
    test_trigger_phrase: str = Form("тестовое сообщение"),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    values_to_save = {
        "test_mode": "true" if test_mode else "false",
        "test_trigger_phrase": test_trigger_phrase.strip() or "тестовое сообщение",
    }
    with SessionLocal() as session:
        save_settings(session, values_to_save)
    return RedirectResponse("/admin?section=settings&saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/chat-settings")
async def update_chat_settings(
    request: Request,
    chat_enabled: str | None = Form(None),
    chat_greeting: str = Form(""),
    operator_user_id: str = Form(""),
    operator_trigger_words: str = Form(""),
    operator_ack: str = Form(""),
    admin_test_user_id: str = Form(""),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        save_settings(session, {
            "chat_enabled": "true" if chat_enabled else "false",
            "chat_greeting": chat_greeting.strip() or settings.chat_greeting,
            "operator_user_id": operator_user_id.strip(),
            "operator_trigger_words": operator_trigger_words.strip() or settings.operator_trigger_words,
            "operator_ack": operator_ack.strip() or settings.operator_ack,
            "admin_test_user_id": admin_test_user_id.strip(),
        })
    return RedirectResponse("/admin?section=chat", status_code=status.HTTP_303_SEE_OTHER)


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


@app.post("/admin/campaigns")
async def save_campaign(
    request: Request,
    post_id: int = Form(...),
    title: str = Form(""),
    promo_code: str = Form(...),
    shop_url: str = Form(...),
    promo_message: str = Form(...),
    enabled: str | None = Form(None),
    stop_words: str = Form(""),
    min_comment_length: int = Form(1),
    one_promo_per_user: str | None = Form(None),
    attachment: UploadFile | None = File(None),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        campaign = session.query(Campaign).filter_by(post_id=post_id).first()
        if campaign is None:
            campaign = Campaign(post_id=post_id)
            session.add(campaign)
        old_path = campaign.attachment_path
        campaign.title = title.strip() or f"Пост {post_id}"
        campaign.promo_code = promo_code.strip()
        campaign.shop_url = shop_url.strip()
        campaign.promo_message = promo_message
        campaign.stop_words = stop_words.strip()
        campaign.min_comment_length = max(0, min_comment_length)
        campaign.one_promo_per_user = bool(one_promo_per_user)
        campaign.enabled = bool(enabled)
        if attachment and attachment.filename:
            if not attachment.content_type:
                return RedirectResponse("/admin?section=campaigns&attachment_error=empty", status_code=status.HTTP_303_SEE_OTHER)
            allowed = attachment.content_type.startswith(("image/", "video/", "audio/")) or attachment.content_type in {"application/pdf", "application/zip", "application/x-zip-compressed", "text/plain"}
            if not allowed:
                return RedirectResponse("/admin?section=campaigns&attachment_error=type", status_code=status.HTTP_303_SEE_OTHER)
            data = await attachment.read()
            if len(data) > 50 * 1024 * 1024:
                return RedirectResponse("/admin?section=campaigns&attachment_error=size", status_code=status.HTTP_303_SEE_OTHER)
            path = ATTACHMENT_DIR / f"campaign_{uuid4().hex}"
            path.write_bytes(data)
            campaign.attachment_path = str(path)
            campaign.attachment_name = attachment.filename
            campaign.attachment_type = attachment.content_type
        session.commit()
    if attachment and attachment.filename and old_path:
        Path(old_path).unlink(missing_ok=True)
    return RedirectResponse(f"/admin?section=campaigns&campaign_saved=1&campaign_id={campaign.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.api_route("/admin/campaigns/{campaign_id}/delete", methods=["GET", "POST"])
async def delete_campaign(request: Request, campaign_id: int):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign:
            session.delete(campaign)
            session.commit()
    return RedirectResponse("/admin?section=campaigns&campaign_deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@app.api_route("/admin/campaigns/{campaign_id}/toggle", methods=["GET", "POST"])
async def toggle_campaign(request: Request, campaign_id: int):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign:
            campaign.enabled = not campaign.enabled
            session.commit()
    return RedirectResponse("/admin?section=campaigns&campaign_toggled=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/test-send")
async def test_send(request: Request):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    campaign_id = form.get("campaign_id")
    with SessionLocal() as session:
        values = read_settings(session)
        campaign = session.get(Campaign, int(campaign_id)) if campaign_id else session.query(Campaign).filter_by(enabled=True).order_by(Campaign.id).first()
    user_id = int(values.get("admin_test_user_id", settings.admin_test_user_id) or 0)
    if user_id <= 0:
        return RedirectResponse("/admin?test_error=no_user", status_code=status.HTTP_303_SEE_OTHER)
    if campaign is None:
        return RedirectResponse("/admin?section=campaigns&test_error=no_campaign", status_code=status.HTTP_303_SEE_OTHER)
    template = campaign.promo_message.replace("\\n", "\n")
    user_name = await get_user_name(user_id)
    text = template.format(
        promo_code=campaign.promo_code,
        shop_url=campaign.shop_url,
        user_name=user_name,
        first_name=user_name,
    )
    attachment = ""
    if campaign.attachment_path and Path(campaign.attachment_path).exists():
        attachment = await upload_file_for_message(
            user_id,
            Path(campaign.attachment_path),
            campaign.attachment_name or "attachment",
            campaign.attachment_type or "application/octet-stream",
        )
    try:
        await send_message(user_id, text, random_id=-1, attachment=attachment)
    except VkApiError:
        return RedirectResponse("/admin?section=campaigns&test_error=vk", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/admin?section=campaigns&test_sent=1&campaign_id={campaign.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/vk/callback", response_class=PlainTextResponse)
async def vk_callback(request: Request) -> str:
    payload = await request.json()

    if payload.get("secret") != settings.vk_callback_secret:
        logger.warning("Rejected callback with invalid secret")
        return "invalid secret"

    if payload.get("type") == "confirmation":
        return settings.vk_confirmation_code
    if payload.get("type") == "message_new":
        await handle_new_message(payload)
        return "ok"
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
        campaign = session.query(Campaign).filter_by(post_id=post_id).first()
        if already_processed(session, comment_id):
            return "ok"
        session.add(ProcessedComment(comment_id=comment_id, post_id=post_id, user_id=user_id))
        session.commit()

    try:
        if campaign is not None and not campaign.enabled:
            update_status(comment_id, "campaign_disabled")
            return "ok"
        test_enabled = as_bool(setting(values, "test_mode", settings.test_mode))
        test_phrase = str(setting(values, "test_trigger_phrase", settings.test_trigger_phrase)).strip().casefold()
        if test_enabled and test_phrase not in comment_text.casefold():
            update_status(comment_id, "test_filtered")
            return "ok"

        min_length = int(campaign.min_comment_length if campaign else (setting(values, "min_comment_length", settings.min_comment_length) or 1))
        if len(comment_text) < min_length:
            update_status(comment_id, "too_short")
            return "ok"

        configured_stop_words = campaign.stop_words if campaign else setting(values, "stop_words", settings.stop_words)
        stop_words = [
            word.strip().casefold()
            for word in str(configured_stop_words).replace(",", "\n").splitlines()
            if word.strip()
        ]
        if any(word in comment_text.casefold() for word in stop_words):
            update_status(comment_id, "stop_word")
            return "ok"

        one_promo_per_user = campaign.one_promo_per_user if campaign else as_bool(setting(values, "one_promo_per_user", settings.one_promo_per_user))
        if one_promo_per_user:
            with SessionLocal() as session:
                if already_sent_to_user(session, user_id):
                    update_status(comment_id, "already_sent")
                    return "ok"

        post_ids = values.get("allowed_post_ids", settings.allowed_post_ids).strip()
        if campaign is not None:
            post_ids = ""
        if post_ids and post_id not in {int(value.strip()) for value in post_ids.split(",") if value.strip()}:
            update_status(comment_id, "post_filtered")
            return "ok"
        if not await is_group_member(user_id):
            update_status(comment_id, "not_member")
            return "ok"

        template = (campaign.promo_message if campaign else values.get("promo_message", settings.promo_message)).replace("\\n", "\n")
        user_name = await get_user_name(user_id)
        text = template.format(
            promo_code=campaign.promo_code if campaign else values.get("promo_code", settings.promo_code),
            shop_url=campaign.shop_url if campaign else values.get("shop_url", settings.shop_url),
            user_name=user_name,
            first_name=user_name,
        )
        attachments = values.get("promo_attachments", settings.promo_attachments)
        attachment_path = campaign.attachment_path if campaign else values.get("attachment_path", "")
        if attachment_path and Path(attachment_path).exists():
            attachments = await upload_file_for_message(
                user_id,
                Path(attachment_path),
                campaign.attachment_name if campaign else values.get("attachment_name", "attachment"),
                campaign.attachment_type if campaign else values.get("attachment_type", "application/octet-stream"),
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


async def handle_new_message(payload: dict) -> None:
    obj = payload.get("object") or {}
    user_id = int(obj.get("from_id", 0))
    if user_id <= 0:
        return
    with SessionLocal() as session:
        values = read_settings(session)
    if not as_bool(values.get("chat_enabled", settings.chat_enabled)):
        return
    message_text = str(obj.get("text", "")).strip()
    triggers = str(values.get("operator_trigger_words", settings.operator_trigger_words)).replace(",", "\n").splitlines()
    wants_operator = any(word.strip().casefold() in message_text.casefold() for word in triggers if word.strip())
    if wants_operator:
        ack = values.get("operator_ack", settings.operator_ack)
        await send_message(user_id, ack, random_id=int(obj.get("conversation_message_id", 0) or obj.get("id", 0)))
        operator_id = int(values.get("operator_user_id", settings.operator_user_id) or 0)
        if operator_id > 0:
            name = await get_user_name(user_id)
            notification = f"Запрос оператора от пользователя {name} (id{user_id}).\nСообщение: {message_text or '[без текста]'}"
            try:
                await send_message(operator_id, notification, random_id=-user_id)
            except VkApiError:
                logger.warning("Could not notify operator %s", operator_id)
        return
    greeting = values.get("chat_greeting", settings.chat_greeting)
    await send_message(user_id, greeting, random_id=int(obj.get("conversation_message_id", 0) or obj.get("id", 0)))


def update_status(comment_id: int, status: str, error: str | None = None) -> None:
    with SessionLocal() as session:
        record = session.query(ProcessedComment).filter_by(comment_id=comment_id).first()
        if record:
            record.status = status
            record.error = error
            session.commit()
