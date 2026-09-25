import logging

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func

from .config import get_settings
from .db import ProcessedComment, SessionLocal, already_processed, init_db, read_settings, save_settings
from .vk_api import VkApiError, is_group_member, send_message

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)
app = FastAPI(title="VK Comment Promo Bot")
templates = Jinja2Templates(directory="app/templates")
app.add_middleware(SessionMiddleware, secret_key=settings.admin_session_secret, https_only=False, max_age=60 * 60 * 12)


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
        "allowed_post_ids": values.get("allowed_post_ids", settings.allowed_post_ids),
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
    allowed_post_ids: str = Form(""),
):
    if not admin_required(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    with SessionLocal() as session:
        save_settings(
            session,
            {
                "promo_code": promo_code.strip(),
                "shop_url": shop_url.strip(),
                "promo_message": promo_message,
                "allowed_post_ids": allowed_post_ids.strip(),
            },
        )
    return RedirectResponse("/admin?saved=1", status_code=status.HTTP_303_SEE_OTHER)


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
    if not comment_id or user_id <= 0:
        return "ok"
    with SessionLocal() as session:
        values = read_settings(session)
        if already_processed(session, comment_id):
            return "ok"
        session.add(ProcessedComment(comment_id=comment_id, post_id=post_id, user_id=user_id))
        session.commit()

    try:
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
        await send_message(user_id, text, random_id=comment_id)
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
