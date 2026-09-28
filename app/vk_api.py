import json
from pathlib import Path

import httpx

from .config import get_settings


class VkApiError(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        prefix = str(message).split(":", 1)[0]
        self.code = int(prefix) if prefix.isdigit() else None


async def call(method: str, **params):
    from .projects import current_project, get_project

    project = current_project.get()
    if project is not None:
        fresh = get_project(project.id)
        if fresh is None or not fresh.enabled:
            raise VkApiError("Проект приостановлен. Включите его на странице проектов.")
    settings = get_settings()
    if not settings.vk_group_token:
        raise VkApiError("В проекте не задан токен сообщества")
    return await _request(method, settings.vk_group_token, **params)


async def _request(method: str, token: str, **params):
    params.update(access_token=token, v=get_settings().vk_api_version)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"https://api.vk.com/method/{method}", data=params)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        error = payload["error"]
        message = str(error.get("error_msg") or "VK API error").replace(
            token, "[redacted]"
        )
        raise VkApiError(f"{error.get('error_code')}: {message}")
    return payload["response"]


async def is_group_member(user_id: int) -> bool:
    result = await call(
        "groups.isMember", group_id=get_settings().vk_group_id, user_id=user_id
    )
    return bool(result)


async def get_user_name(user_id: int) -> str:
    result = await call("users.get", user_ids=user_id, fields="first_name")
    if result and result[0].get("first_name"):
        return result[0]["first_name"]
    return "друг"


async def is_messages_allowed(user_id: int) -> bool:
    result = await call(
        "messages.isMessagesFromGroupAllowed",
        group_id=get_settings().vk_group_id,
        user_id=user_id,
    )
    return isinstance(result, dict) and result.get("is_allowed") == 1


async def reply_to_wall_comment(comment, message: str, guid: str) -> None:
    # video.createComment does not accept community tokens. Never silently post
    # to a wall with a video's numeric ID instead.
    if comment.source_type != "wall":
        raise ValueError("Приглашения доступны только для комментариев к постам")
    await call(
        "wall.createComment",
        owner_id=comment.owner_id,
        post_id=comment.object_id,
        reply_to_comment=comment.comment_id,
        from_group=get_settings().vk_group_id,
        message=message,
        guid=guid,
    )


def video_reply_available() -> bool:
    from .projects import current_project, get_project

    selected = current_project.get()
    fresh = get_project(selected.id) if selected else None
    return bool(fresh and fresh.enabled and fresh.video_token)


async def reply_to_video_comment(comment, message: str, guid: str) -> None:
    """Use the owner's optional user credential ONLY for this API operation.

    VK's official video schema accepts a user token and from_group=1. Never
    publish as the personal account or use this token to bypass DM permissions.
    """
    from .projects import current_project, get_project

    selected = current_project.get()
    project = get_project(selected.id) if selected else None
    if not project or not project.enabled or not project.video_token:
        raise VkApiError("Для ответа под видео подключите токен в настройках проекта")
    if comment.source_type != "video" or comment.owner_id != -project.group_id:
        raise ValueError("Видео должно принадлежать текущему сообществу")
    await _request(
        "video.createComment",
        project.video_token,
        owner_id=comment.owner_id,
        video_id=comment.object_id,
        reply_to_comment=comment.comment_id,
        from_group=1,
        message=message,
        guid=guid,
    )


async def send_message(
    user_id: int,
    text: str,
    random_id: int,
    attachment: str = "",
    keyboard: dict | None = None,
) -> None:
    params = {"user_id": user_id, "random_id": random_id, "message": text}
    if attachment.strip():
        params["attachment"] = attachment.strip()
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    await call("messages.send", **params)
    from .clients import record_outgoing

    record_outgoing(user_id, random_id)


async def upload_file_for_message(
    user_id: int, path: Path, filename: str, content_type: str
) -> str:
    if content_type.startswith("image/"):
        server = await call("photos.getMessagesUploadServer", peer_id=user_id)
        field_name = "photo"
        save_method = "photos.saveMessagesPhoto"
    else:
        # video.save accepts user tokens only. Community bots send media as docs.
        server = await call("docs.getMessagesUploadServer", type="doc", peer_id=user_id)
        field_name = "file"
        save_method = "docs.save"

    async with httpx.AsyncClient(timeout=180) as client:
        with path.open("rb") as file_handle:
            response = await client.post(
                server["upload_url"],
                files={field_name: (filename, file_handle, content_type)},
            )
    response.raise_for_status()
    uploaded = response.json()
    if "error" in uploaded:
        error = uploaded["error"]
        raise VkApiError(f"{error.get('error_code')}: {error.get('error_msg')}")

    if save_method == "photos.saveMessagesPhoto":
        save_params = {
            "server": uploaded.get("server"),
            "hash": uploaded.get("hash"),
            "photo": uploaded.get("photo"),
        }
        if uploaded.get("files"):
            save_params["photo"] = json.dumps(uploaded["files"], separators=(",", ":"))
        saved = await call(save_method, **save_params)
        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"

    saved = await call(save_method, file=uploaded["file"], title=filename[:128])
    document = saved.get("doc") or saved[0]
    return f"doc{document['owner_id']}_{document['id']}"
