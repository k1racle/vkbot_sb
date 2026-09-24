import httpx

from .config import get_settings


class VkApiError(RuntimeError):
    pass


async def call(method: str, **params):
    settings = get_settings()
    params.update(access_token=settings.vk_group_token, v=settings.vk_api_version)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(f"https://api.vk.com/method/{method}", data=params)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        error = payload["error"]
        raise VkApiError(f"{error.get('error_code')}: {error.get('error_msg')}")
    return payload["response"]


async def is_group_member(user_id: int) -> bool:
    result = await call("groups.isMember", group_id=get_settings().vk_group_id, user_id=user_id)
    return bool(result)


async def send_message(user_id: int, text: str, random_id: int) -> None:
    await call("messages.send", user_id=user_id, random_id=random_id, message=text)
