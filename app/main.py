import hashlib
import logging
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from .comments import Comment, normalize_comment
from .config import get_settings
from .db import (
    Campaign,
    ProcessedComment,
    SessionLocal,
    already_processed,
    init_db,
    read_settings,
    save_settings,
)
from .dialog import (
    CALLBACK_SLOTS,
    delivered,
    handle_group_join,
    handle_message,
    handle_operator_reply,
    user_lock,
)
from .flows import render
from .gifts import DEFAULT_INVITATIONS, invite_to_chat
from .operators import parse_operator_ids
from .scenario_api import router as scenario_router
from .vk_api import (
    VkApiError,
    get_user_name,
    is_group_member,
    send_message,
    upload_file_for_message,
)

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)
app = FastAPI(title="VK Бот")
templates = Jinja2Templates(directory="app/templates")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_session_secret,
    https_only=False,
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(scenario_router)
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
        "login.html",
        {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    campaign_id: int | None = None,
    new_campaign: bool = False,
    section: str = "scenarios",
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if section not in {
        "settings",
        "campaigns",
        "chat",
        "stats",
        "scenarios",
        "clients",
    }:
        section = "scenarios"
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    with SessionLocal() as session:
        values = read_settings(session)
        total = session.query(func.count(ProcessedComment.id)).scalar() or 0
        sent = (
            session.query(func.count(ProcessedComment.id))
            .filter_by(status="sent")
            .scalar()
            or 0
        )
        failed = (
            session.query(func.count(ProcessedComment.id))
            .filter(ProcessedComment.status.in_(["failed", "gift_failed"]))
            .scalar()
            or 0
        )
        status_counts = dict(
            session.query(ProcessedComment.status, func.count(ProcessedComment.id))
            .group_by(ProcessedComment.status)
            .all()
        )
        recent = (
            session.query(ProcessedComment)
            .order_by(ProcessedComment.id.desc())
            .limit(30)
            .all()
        )
        campaigns = session.query(Campaign).order_by(Campaign.post_id.desc()).all()
        selected_campaign = (
            None
            if new_campaign
            else (
                session.get(Campaign, campaign_id)
                if campaign_id
                else (campaigns[0] if campaigns else None)
            )
        )
    form = {
        "test_mode": as_bool(values.get("test_mode", str(settings.test_mode))),
        "test_trigger_phrase": values.get(
            "test_trigger_phrase", settings.test_trigger_phrase
        ),
        "admin_test_user_id": values.get(
            "admin_test_user_id", settings.admin_test_user_id
        ),
        "chat_enabled": as_bool(
            values.get("chat_enabled") or settings.chat_enabled or "true"
        ),
        "chat_greeting": values.get("chat_greeting") or settings.chat_greeting,
        "operator_user_id": values.get("operator_user_id", settings.operator_user_id),
        "operator_trigger_words": values.get(
            "operator_trigger_words", settings.operator_trigger_words
        ),
        "operator_ack": values.get("operator_ack", settings.operator_ack),
    }
    campaign_form = {
        "id": selected_campaign.id if selected_campaign else "",
        "post_id": (selected_campaign.post_id or "") if selected_campaign else "",
        "title": selected_campaign.title if selected_campaign else "",
        "promo_code": selected_campaign.promo_code if selected_campaign else "",
        "shop_url": selected_campaign.shop_url if selected_campaign else "",
        "promo_message": selected_campaign.promo_message if selected_campaign else "",
        "enabled": selected_campaign.enabled if selected_campaign else True,
        "delivery_mode": selected_campaign.delivery_mode
        if selected_campaign
        else "direct",
        "public_reply_variants": (
            selected_campaign.public_reply_variants if selected_campaign else None
        )
        or DEFAULT_INVITATIONS,
        "attachment_name": selected_campaign.attachment_name
        if selected_campaign
        else "",
        "stop_words": selected_campaign.stop_words if selected_campaign else "",
        "min_comment_length": selected_campaign.min_comment_length
        if selected_campaign
        else 1,
        "one_promo_per_user": selected_campaign.one_promo_per_user
        if selected_campaign
        else True,
    }
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "section": section,
            "csrf": request.session["csrf"],
            "form": form,
            "campaign_form": campaign_form,
            "stats": {
                "total": total,
                "sent": sent,
                "failed": failed,
                "status_counts": status_counts,
            },
            "recent": recent,
            "campaigns": campaigns,
        },
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
    return RedirectResponse(
        "/admin?section=settings&saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


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
    try:
        operator_user_id = ", ".join(map(str, parse_operator_ids(operator_user_id)))
    except ValueError:
        return RedirectResponse(
            "/admin?section=chat&settings_error=operators",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    with SessionLocal() as session:
        save_settings(
            session,
            {
                "chat_enabled": "true" if chat_enabled else "false",
                "chat_greeting": chat_greeting.strip() or settings.chat_greeting,
                "operator_user_id": operator_user_id.strip(),
                "operator_trigger_words": operator_trigger_words.strip()
                or settings.operator_trigger_words,
                "operator_ack": operator_ack.strip() or settings.operator_ack,
                "admin_test_user_id": admin_test_user_id.strip(),
            },
        )
    return RedirectResponse(
        "/admin?section=chat&saved=1", status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/campaigns")
async def save_campaign(
    request: Request,
    post_id: str = Form(""),
    title: str = Form(""),
    promo_code: str = Form(...),
    shop_url: str = Form(...),
    promo_message: str = Form(...),
    enabled: str | None = Form(None),
    stop_words: str = Form(""),
    min_comment_length: int = Form(1),
    one_promo_per_user: str | None = Form(None),
    delivery_mode: str = Form("direct"),
    public_reply_variants: list[str] | None = Form(None),
    attachment: UploadFile | None = File(None),
    campaign_id: str = Form(""),
    remove_attachment: str | None = Form(None),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    variants = (
        [value.strip() for value in public_reply_variants if value.strip()]
        if public_reply_variants is not None
        else list(DEFAULT_INVITATIONS)
    )
    if (
        delivery_mode not in {"direct", "chat_invite"}
        or (
            delivery_mode == "chat_invite"
            and (
                not 1 <= len(variants) <= 10
                or any(
                    len(value) > 2000 or "{chat_url}" not in value for value in variants
                )
            )
        )
        or len(variants) > 10
        or any(len(value) > 2000 for value in variants)
    ):
        return RedirectResponse(
            "/admin?section=campaigns&campaign_error=invitation", status_code=303
        )
    entered_post_id = post_id.strip()
    if entered_post_id and (
        not entered_post_id.isascii()
        or not entered_post_id.isdigit()
        or len(entered_post_id) > 10
        or not 0 < int(entered_post_id) <= 2147483647
    ):
        return RedirectResponse(
            "/admin?section=campaigns&campaign_error=post_id", status_code=303
        )
    post_id = int(entered_post_id) if entered_post_id else 0
    with SessionLocal() as session:
        campaign = (
            session.get(Campaign, int(campaign_id)) if campaign_id.isdigit() else None
        )
        duplicate = session.query(Campaign).filter_by(post_id=post_id).first()
        if duplicate and (not campaign or duplicate.id != campaign.id):
            return RedirectResponse(
                "/admin?section=campaigns&campaign_error="
                + ("duplicate" if post_id else "duplicate_general"),
                status_code=303,
            )
        if campaign is None:
            campaign = Campaign(post_id=post_id)
            session.add(campaign)
        old_path = campaign.attachment_path
        campaign.post_id = post_id
        campaign.title = title.strip() or (
            f"Пост {post_id}" if post_id else "Все публикации"
        )
        campaign.promo_code = promo_code.strip()
        campaign.shop_url = shop_url.strip()
        campaign.promo_message = promo_message
        campaign.stop_words = stop_words.strip()
        campaign.min_comment_length = max(0, min_comment_length)
        campaign.one_promo_per_user = bool(one_promo_per_user)
        campaign.delivery_mode = delivery_mode
        campaign.public_reply_variants = variants
        campaign.enabled = bool(enabled)
        if remove_attachment:
            campaign.attachment_path = campaign.attachment_name = (
                campaign.attachment_type
            ) = ""
        if attachment and attachment.filename:
            if not attachment.content_type:
                return RedirectResponse(
                    "/admin?section=campaigns&attachment_error=empty",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            allowed = attachment.content_type.startswith(
                ("image/", "video/", "audio/")
            ) or attachment.content_type in {
                "application/pdf",
                "application/zip",
                "application/x-zip-compressed",
                "text/plain",
            }
            if not allowed:
                return RedirectResponse(
                    "/admin?section=campaigns&attachment_error=type",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            data = await attachment.read(50 * 1024 * 1024 + 1)
            if len(data) > 50 * 1024 * 1024:
                return RedirectResponse(
                    "/admin?section=campaigns&attachment_error=size",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            path = ATTACHMENT_DIR / f"campaign_{uuid4().hex}"
            path.write_bytes(data)
            campaign.attachment_path = str(path)
            campaign.attachment_name = attachment.filename
            campaign.attachment_type = attachment.content_type
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return RedirectResponse(
                "/admin?section=campaigns&campaign_error="
                + ("duplicate" if post_id else "duplicate_general"),
                status_code=303,
            )
    if ((attachment and attachment.filename) or remove_attachment) and old_path:
        Path(old_path).unlink(missing_ok=True)
    return RedirectResponse(
        f"/admin?section=campaigns&campaign_saved=1&campaign_id={campaign.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/campaigns/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign:
            session.delete(campaign)
            session.commit()
    return RedirectResponse(
        "/admin?section=campaigns&campaign_deleted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/campaigns/{campaign_id}/toggle")
async def toggle_campaign(request: Request, campaign_id: int):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign:
            campaign.enabled = not campaign.enabled
            session.commit()
    return RedirectResponse(
        "/admin?section=campaigns&campaign_toggled=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/test-send")
async def test_send(request: Request):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    form = await request.form()
    campaign_id = str(form.get("campaign_id") or "")
    with SessionLocal() as session:
        values = read_settings(session)
        campaign = (
            session.get(Campaign, int(campaign_id)) if campaign_id.isdigit() else None
        )
    candidate = str(
        form.get("user_id")
        or values.get("admin_test_user_id", settings.admin_test_user_id)
        or ""
    )
    user_id = int(candidate) if candidate.isdigit() else 0
    if user_id <= 0:
        return RedirectResponse(
            "/admin?section=campaigns&test_error=no_user",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if campaign is None:
        return RedirectResponse(
            "/admin?section=campaigns&test_error=no_campaign",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        user_name = await get_user_name(user_id)
        message = render(
            campaign.promo_message,
            {
                "promo_code": campaign.promo_code,
                "shop_url": campaign.shop_url,
                "user_name": user_name,
                "first_name": user_name,
            },
        )
        attachment = ""
        if campaign.attachment_path and Path(campaign.attachment_path).exists():
            attachment = await upload_file_for_message(
                user_id,
                Path(campaign.attachment_path),
                campaign.attachment_name,
                campaign.attachment_type,
            )
        await send_message(
            user_id,
            message,
            random_id=secrets.randbelow(2147483646) + 1,
            attachment=attachment,
        )
    except Exception:
        logger.exception("Test send failed")
        return RedirectResponse(
            "/admin?section=campaigns&test_error=vk",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        f"/admin?section=campaigns&test_sent=1&campaign_id={campaign.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/vk/callback", response_class=PlainTextResponse)
async def vk_callback(request: Request) -> str:
    payload = await request.json()

    if payload.get("secret") != settings.vk_callback_secret:
        logger.warning("Rejected callback with invalid secret")
        return "invalid secret"

    if payload.get("type") == "confirmation":
        return settings.vk_confirmation_code
    if payload.get("type") == "message_new":
        await handle_message(payload)
        return "ok"
    if payload.get("type") == "message_reply":
        await handle_operator_reply(payload)
        return "ok"
    if payload.get("type") == "group_join":
        await handle_group_join(payload)
        return "ok"
    comment = normalize_comment(payload, settings.vk_group_id)
    if comment is None:
        return "ok"
    user_id = comment.user_id
    async with user_lock(user_id), CALLBACK_SLOTS:
        with SessionLocal() as guard:
            if guard.bind.dialect.name == "postgresql":
                guard.execute(
                    sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_id}
                )
            return await process_comment(comment)


async def process_comment(comment: Comment) -> str:
    comment_id, post_id, user_id = (
        comment.comment_id,
        comment.object_id,
        comment.user_id,
    )
    comment_text, event_key = comment.text, comment.event_key
    with SessionLocal() as session:
        values = read_settings(session)
        campaign = None
        if comment.source_type == "wall":
            campaign = session.query(Campaign).filter_by(post_id=post_id).first()
        # A disabled dedicated campaign is an explicit exclusion, not an invitation
        # to issue another campaign's promo. Videos only use the general campaign.
        if campaign is None:
            campaign = session.query(Campaign).filter_by(post_id=0).first()
        if already_processed(session, event_key):
            return "ok"
        session.add(
            ProcessedComment(
                event_key=event_key,
                source_type=comment.source_type,
                owner_id=comment.owner_id,
                comment_id=comment_id,
                post_id=post_id,
                user_id=user_id,
                campaign_id=campaign.id if campaign else None,
            )
        )
        session.commit()

    try:
        if campaign is None:
            update_status(event_key, "no_campaign")
            return "ok"
        if campaign is not None and not campaign.enabled:
            update_status(event_key, "campaign_disabled")
            return "ok"
        test_enabled = as_bool(setting(values, "test_mode", settings.test_mode))
        test_phrase = (
            str(setting(values, "test_trigger_phrase", settings.test_trigger_phrase))
            .strip()
            .casefold()
        )
        if test_enabled and test_phrase not in comment_text.casefold():
            update_status(event_key, "test_filtered")
            return "ok"

        min_length = campaign.min_comment_length
        if len(comment_text) < min_length:
            update_status(event_key, "too_short")
            return "ok"

        configured_stop_words = campaign.stop_words
        stop_words = [
            word.strip().casefold()
            for word in str(configured_stop_words).replace(",", "\n").splitlines()
            if word.strip()
        ]
        if any(word in comment_text.casefold() for word in stop_words):
            update_status(event_key, "stop_word")
            return "ok"

        one_promo_per_user = campaign.one_promo_per_user
        if one_promo_per_user:
            with SessionLocal() as session:
                if delivered(session, user_id, campaign):
                    update_status(event_key, "already_sent")
                    return "ok"

        if campaign.delivery_mode == "chat_invite":
            # Non-members may earn a pending gift too. Membership is required
            # when claiming it, not when opening the path into the chat.
            with SessionLocal() as session:
                await invite_to_chat(session, comment, campaign)
            return "ok"

        if not await is_group_member(user_id):
            update_status(event_key, "not_member")
            return "ok"

        template = campaign.promo_message
        user_name = await get_user_name(user_id)
        text = render(
            template,
            {
                "promo_code": campaign.promo_code,
                "shop_url": campaign.shop_url,
                "user_name": user_name,
                "first_name": user_name,
            },
        )
        attachments = ""
        attachment_path = campaign.attachment_path
        if attachment_path and Path(attachment_path).exists():
            attachments = await upload_file_for_message(
                user_id,
                Path(attachment_path),
                campaign.attachment_name,
                campaign.attachment_type,
            )
        random_id = (
            int.from_bytes(
                hashlib.sha256(f"comment:{event_key}:{user_id}".encode()).digest()[:4],
                "big",
            )
            & 0x7FFFFFFF
        )
        await send_message(
            user_id, text, random_id=random_id or 1, attachment=attachments
        )
        update_status(event_key, "sent")
        logger.info("Promo sent: event=%s user=%s", event_key, user_id)
    except VkApiError as error:
        update_status(event_key, "failed", str(error))
        logger.warning("VK rejected message for user %s: %s", user_id, error)
    except Exception as error:
        update_status(event_key, "failed", str(error))
        logger.exception("Failed to process comment %s", event_key)

    return "ok"


def update_status(event_key: str, status: str, error: str | None = None) -> None:
    with SessionLocal() as session:
        record = session.query(ProcessedComment).filter_by(event_key=event_key).first()
        if record:
            record.status = status
            record.error = error
            session.commit()
