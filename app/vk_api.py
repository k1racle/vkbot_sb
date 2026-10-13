import asyncio
import json
import time
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


async def safe_comments_snapshot(post_id: int) -> list[dict]:
    """Read the complete post and its threads using the optional user credential.

    VK wall.getComments does not accept community credentials. The user token
    is used only for reading here; all invitations/prizes still use the group.
    An incomplete or changing page is an error, never evidence of a deletion.
    """
    from .comments import integer
    from .projects import current_project, get_project

    selected = current_project.get()
    project = get_project(selected.id) if selected else None
    if not project or not project.enabled or not project.video_token:
        raise VkApiError(
            "Для проверки сейфа подключите пользовательский токен в настройках проекта"
        )
    token = project.video_token
    owner = -project.group_id
    last_request = None

    async def pages(parent=0):
        nonlocal last_request
        result, seen = [], set()
        offset, total = 0, None
        while True:
            params = dict(
                owner_id=owner,
                post_id=post_id,
                count=100,
                offset=offset,
                sort="asc",
                preview_length=0,
                thread_items_count=10,
            )
            if parent:
                params["comment_id"] = parent
            # Large games require many pages. Avoid sending a burst of requests
            # under the user's credential; VK errors still fail verification.
            if last_request is not None:
                await asyncio.sleep(max(0, 0.35 - (time.monotonic() - last_request)))
            last_request = time.monotonic()
            data = await _request("wall.getComments", token, **params)
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("items"), list)
                or type(data.get("count")) is not int
            ):
                raise VkApiError(
                    "VK вернул неполный список комментариев; повторите проверку"
                )
            count = data.get("current_level_count", data["count"])
            if type(count) is not int:
                raise VkApiError("VK вернул некорректное количество комментариев")
            if count < 0 or (total is not None and total != count):
                raise VkApiError(
                    "Комментарии изменились во время проверки; повторите её"
                )
            total = count
            batch = data["items"]
            for item in batch:
                ident = integer(item.get("id")) if isinstance(item, dict) else 0
                if ident <= 0 or ident in seen:
                    raise VkApiError(
                        "VK вернул неполную страницу комментариев; повторите проверку"
                    )
                seen.add(ident)
                result.append(item)
            offset += len(batch)
            if offset >= total:
                if offset != total:
                    raise VkApiError(
                        "Количество комментариев изменилось; повторите проверку"
                    )
                return result
            if not batch or offset > 100000:
                raise VkApiError(
                    "Не удалось прочитать все комментарии; повторите проверку"
                )

    roots = await pages()
    result = list(roots)
    for item in roots:
        thread = item.get("thread") or {}
        count = integer(thread.get("count"))
        if count > 0:
            preview = thread.get("items", [])
            result.extend(
                preview
                if isinstance(preview, list) and len(preview) == count
                else await pages(item["id"])
            )
    if len({item["id"] for item in result}) != len(result):
        raise VkApiError("VK вернул повторяющиеся комментарии; повторите проверку")
    return result


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


async def mention_video_author(comment, post_id: int, message: str, guid: str):
    """Mention on a configured wall post; notification delivery is VK's decision."""
    group_id = get_settings().vk_group_id
    if comment.source_type != "video" or comment.owner_id != -group_id or post_id <= 0:
        raise ValueError("Invalid video mention destination")
    await call(
        "wall.createComment",
        owner_id=-group_id,
        post_id=post_id,
        from_group=group_id,
        message=f"[id{comment.user_id}|Ответ на ваш комментарий к видео]: {message}",
        guid=guid,
    )


async def reply_to_video_comment(comment, message: str, guid: str) -> None:
    """Use the owner's optional user credential for a video reply.

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
