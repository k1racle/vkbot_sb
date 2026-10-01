"""Isolated browser review; all VK calls are stubbed, no external messages."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def serve(directory):
    os.environ.update(
        VK_GROUP_ID="123",
        VK_GROUP_TOKEN="review-only",
        VK_CALLBACK_SECRET="review-secret",
        VK_CONFIRMATION_CODE="review-confirm",
        ADMIN_USERNAME="preview",
        ADMIN_PASSWORD="preview-local-only",
        ADMIN_SESSION_SECRET="review-session-only",
        DATABASE_URL=f"sqlite:///{directory}/review.db",
        PROJECTS_KEY_FILE=f"{directory}/key",
        CHAT_ENABLED="true",
        BACKGROUND_JOBS_ENABLED="false",
        PUBLIC_BASE_URL="http://127.0.0.1:8766",
    )
    from app import main, vk_api, project_web
    import uvicorn

    async def send(*args, **kwargs):
        return 1

    async def name(*args, **kwargs):
        return "Анна"

    async def group(*args, **kwargs):
        return {"id": 123, "name": "Тестовый проект"}

    async def query(method, token, **params):
        if method == "groups.getById":
            return {"groups": [{"id": 123, "is_admin": 1}]}
        if method == "groups.getTokenPermissions":
            return {"permissions": [{"name": "messages", "setting": 1}]}
        if method == "groups.getCallbackServers":
            return {
                "items": [
                    {
                        "id": 1,
                        "url": "http://127.0.0.1:8766/vk/callback",
                        "status": "ok",
                        "secret_key": "review-secret",
                    }
                ]
            }
        if method == "groups.getCallbackSettings":
            return {"events": {"message_new": 1, "video_comment_new": 1}}
        raise AssertionError(f"Unexpected VK call: {method}")

    vk_api.send_message, vk_api.get_user_name, vk_api._request = send, name, query
    project_web.fetch_group = group
    uvicorn.run(main.app, host="127.0.0.1", port=8766, log_level="warning")


def review():
    from playwright.sync_api import sync_playwright

    base = "http://127.0.0.1:8766"
    output = Path("data/review-priority")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vkbot-review-") as directory:
        process = subprocess.Popen([sys.executable, __file__, "--serve", directory])
        try:
            for _ in range(80):
                try:
                    urlopen(base + "/login", timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1536, "height": 1024})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.url.startswith(base)
                        else route.abort()
                    ),
                )
                page.goto(base + "/login")
                page.locator('[name="username"]').fill("preview")
                page.locator('[name="password"]').fill("preview-local-only")
                page.locator('button[type="submit"]').click()
                page.wait_for_url("**/projects")
                page.goto(base + "/admin?section=settings")
                page.locator("#check-vk").click()
                page.locator("#vk-diagnostics").get_by_text(
                    "Ключ принадлежит этому сообществу", exact=True
                ).wait_for()
                page.screenshot(path=str(output / "diagnostics.png"), full_page=True)
                page.goto(base + "/admin?section=scenarios")
                page.locator("#new-flow").click()
                page.locator('.flow-node[data-id="welcome"]').wait_for()
                page.locator('[data-add="contact"]').click()
                inspector = page.locator("#block-inspector")
                inspector.locator('[data-contact-kind="email"]').check()
                inspector.locator('[data-contact-kind="messenger"]').check()
                inspector.locator('[data-field="notify_manager"]').check()
                assert (
                    inspector.locator('[data-field="variable"]').input_value()
                    == "contact"
                )
                page.screenshot(path=str(output / "contact-block.png"), full_page=True)
                page.evaluate("""async () => {
                    const f = await Admin.api('/scenarios', 'POST');
                    const graph = {nodes: [{id:'start',type:'start',next:'contacts'}, {id:'contacts',type:'contact',text:'Оставьте контакты',variable:'contact',contact_types:['phone','email','messenger']}]};
                    const saved = await Admin.api(`/scenarios/${f.id}`, 'PUT', {title:'Заявка на консультацию',graph,revision:f.revision});
                    await Admin.api(`/scenarios/${f.id}/publish`, 'POST', {revision:saved.revision});
                }""")
                for ident, text in enumerate(
                    ["привет", "+79991234567 anna@example.com https://t.me/anna"], 1
                ):
                    response = page.request.post(
                        base + "/vk/callback",
                        data={
                            "group_id": 123,
                            "secret": "review-secret",
                            "type": "message_new",
                            "event_id": str(ident),
                            "object": {
                                "message": {
                                    "from_id": 77,
                                    "peer_id": 77,
                                    "conversation_message_id": ident,
                                    "text": text,
                                }
                            },
                        },
                    )
                    assert response.ok, response.text()
                page.goto(base + "/admin?section=clients")
                page.locator('[data-client="77"]').click()
                page.locator('.contact-card input[aria-label="Email"]').wait_for()
                assert (
                    page.locator(
                        '.contact-card input[aria-label="Email"]'
                    ).input_value()
                    == "anna@example.com"
                )
                page.locator("#crm-detail .journey summary").first.click()
                page.get_by_text("Сценарий завершён", exact=True).wait_for()
                page.screenshot(path=str(output / "client-history.png"), full_page=True)
                page.locator("#crm-detail [data-close-dialog]").click()
                page.goto(base + "/admin?section=chat")
                page.locator('[name="notify_contacts"]').check()
                page.locator('[name="notify_completed"]').check()
                page.locator('[name="operator_user_id"]').fill("99")
                page.locator(
                    'form[action$="chat-settings"] button[type="submit"]'
                ).click()
                assert page.locator('[name="notify_contacts"]').is_checked()
                assert page.locator('[name="notify_completed"]').is_checked()
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(base + "/admin?section=settings")
                page.locator("#check-vk").click()
                page.locator("#vk-diagnostics").get_by_text(
                    "Ключ принадлежит этому сообществу", exact=True
                ).wait_for()
                page.screenshot(
                    path=str(output / "diagnostics-mobile.png"), full_page=True
                )
                assert not errors, errors
                browser.close()
            print("Browser priority checks passed", flush=True)
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    serve(sys.argv[2]) if len(sys.argv) > 1 and sys.argv[1] == "--serve" else review()
