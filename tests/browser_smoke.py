"""Run against the isolated local review server, never against the live bot."""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    output = Path("data/review")
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1536, "height": 1024}, device_scale_factor=1
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto("http://127.0.0.1:8765/login")
        page.screenshot(path=str(output / "login.png"), full_page=True)
        page.locator('[name="username"]').fill("preview")
        page.locator('[name="password"]').fill("preview-local-only")
        page.locator('button[type="submit"]').click()
        page.wait_for_url("**/admin")
        page.locator("#new-flow").click()
        page.locator('.flow-node[data-id="welcome"]').wait_for()
        page.locator('.flow-node[data-id="welcome"] .node-heading').click()
        # Moving a block at a scaled canvas must move its edges with it.
        heading = page.locator('.flow-node[data-id="welcome"] .node-heading')
        position = heading.bounding_box()
        before = page.locator('.flow-node[data-id="welcome"]').evaluate(
            "el=>parseFloat(el.style.left)"
        )
        page.mouse.move(position["x"] + 60, position["y"] + 12)
        page.mouse.down()
        page.mouse.move(position["x"] + 90, position["y"] + 32, steps=8)
        page.mouse.up()
        after = page.locator('.flow-node[data-id="welcome"]').evaluate(
            "el=>parseFloat(el.style.left)"
        )
        assert after > before + 20
        page.screenshot(path=str(output / "scenario.png"), full_page=True)
        page.locator('#block-inspector [data-field="text"]').fill(
            "Привет, {first_name}! Чем можем помочь?"
        )
        page.locator("#save-flow").click()
        page.wait_for_function(
            "document.querySelector('#dirty-state').textContent.includes('Все изменения')"
        )
        page.locator("#preview-flow").click()
        page.locator("#preview-dialog[open]").wait_for()
        page.get_by_text("Привет, Анна! Чем можем помочь?", exact=True).wait_for()
        page.locator("#preview-buttons").get_by_text(
            "Подобрать товар", exact=True
        ).click()
        page.get_by_text("Расскажите, что ищете?", exact=True).wait_for()
        page.locator("#preview-text").fill("Интересует платье")
        page.locator("#preview-form button").click()
        page.get_by_text(
            "Спасибо! Ваш запрос: Интересует платье. Передаю менеджеру.", exact=True
        ).wait_for()
        page.screenshot(path=str(output / "preview.png"), full_page=True)
        page.locator("#close-preview").click()
        page.locator("#publish-flow").click()
        page.locator("#flow-status").get_by_text("Опубликован", exact=False).wait_for()
        # Editing must still affect the current graph after save and publish.
        page.locator('.flow-node[data-id="welcome"] .node-heading').click()
        page.locator('#block-inspector [data-field="text"]').fill(
            "После публикации — новый черновик"
        )
        page.locator("#save-flow").click()
        page.wait_for_function(
            "document.querySelector('#dirty-state').textContent.includes('Все изменения')"
        )
        page.reload()
        page.locator('.flow-node[data-id="welcome"] .node-content').get_by_text(
            "После публикации — новый черновик", exact=True
        ).wait_for()
        page.locator('.flow-node[data-id="welcome"] .node-heading').click()
        page.locator('#block-inspector [data-field="text"]').fill(
            "Привет, {first_name}! Рады видеть вас в SARKISIAN. Чем можем помочь?"
        )
        page.locator("#save-flow").click()
        page.wait_for_function(
            "document.querySelector('#dirty-state').textContent.includes('Все изменения')"
        )
        page.locator('[data-add="end"]').click()
        page.locator("#validate-flow").click()
        page.locator("#flow-errors").get_by_text(
            "Завершение: блок не соединён со стартом.", exact=True
        ).wait_for()
        end_id = page.locator('.flow-node[data-kind="end"]').get_attribute("data-id")
        # Draw a new connection by clicking an output then an input port.
        page.locator('.flow-node[data-id="thanks"] [data-output="next"]').click()
        page.locator(f'.flow-node[data-id="{end_id}"] [data-input]').click()
        page.locator('.flow-node[data-id="thanks"] .node-heading').click()
        assert (
            page.locator('#block-inspector [data-field="next"]').input_value() == end_id
        )
        page.locator('#block-inspector [data-field="next"]').select_option("manager")
        page.locator(f'.flow-node[data-id="{end_id}"] .node-heading').click()
        page.locator("#delete-node").click()
        page.locator("#save-flow").click()
        page.wait_for_function(
            "document.querySelector('#dirty-state').textContent.includes('Все изменения')"
        )
        page.goto("http://127.0.0.1:8765/admin?section=campaigns&new_campaign=1")
        page.locator('[name="title"]').fill("Коллекция осень · тест интерфейса")
        page.locator('[name="post_id"]').fill(str(int(time.time()) % 1000000000))
        page.locator('[name="promo_code"]').fill("AUTUMN10")
        page.locator('[name="shop_url"]').fill("https://sarkisianbrand.ru/")
        page.locator('[name="promo_message"]').fill(
            "Привет, {first_name}! Ваш промокод {promo_code}"
        )
        page.screenshot(path=str(output / "campaign.png"), full_page=True)
        # No VK sends: only save local campaign data.
        page.get_by_role("button", name="Сохранить кампанию", exact=True).click()
        page.wait_for_url("**campaign*", wait_until="networkidle")
        page.locator("#page-notice").get_by_text(
            "Кампания сохранена.", exact=True
        ).wait_for()
        page.goto("http://127.0.0.1:8765/admin?section=campaigns&new_campaign=1")
        page.locator('[name="title"]').fill("Общая акция — все публикации")
        assert page.locator('[name="post_id"]').input_value() == ""
        assert not page.locator('[name="post_id"]').evaluate("el => el.required")
        page.locator('[name="promo_code"]').fill("ALL10")
        page.locator('[name="shop_url"]').fill("https://sarkisianbrand.ru/")
        page.locator('[name="promo_message"]').fill("Ваш промокод: {promo_code}")
        page.get_by_role("button", name="Сохранить кампанию", exact=True).click()
        page.wait_for_url("**campaign_saved=1**")
        page.locator(".campaign-item.selected small").get_by_text(
            "Все публикации", exact=True
        ).wait_for()
        assert page.locator('[name="post_id"]').input_value() == ""
        page.screenshot(path=str(output / "general-campaign.png"), full_page=True)
        # Configure public invitations without sending any VK comments/messages.
        page.locator("#delivery-mode").select_option("chat_invite")
        assert page.locator("#invitation-settings").is_visible()
        assert page.locator(".invitation-variant").count() == 4
        first_invitation = page.locator('[name="public_reply_variants"]').first
        first_invitation.fill("Здесь забыли ссылку")
        assert not first_invitation.evaluate("el => el.checkValidity()")
        page.locator("#delivery-mode").select_option("direct")
        assert page.locator("#invitation-settings").is_hidden()
        assert first_invitation.evaluate("el => el.checkValidity()")
        page.locator("#delivery-mode").select_option("chat_invite")
        assert first_invitation.input_value() == "Здесь забыли ссылку"
        page.locator('[name="public_reply_variants"]').first.fill(
            "Спасибо за активность 💚 Ваш подарок: {chat_url}\nНапишите «Подарок» в чате."
        )
        page.locator("#add-invitation").click()
        assert page.locator(".invitation-variant").count() == 5
        page.locator("[data-remove-invitation]").last.click()
        assert page.locator(".invitation-variant").count() == 4
        for _ in range(6):
            page.locator("#add-invitation").click()
        assert page.locator(".invitation-variant").count() == 10
        assert page.locator("#add-invitation").is_disabled()
        for _ in range(6):
            page.locator("[data-remove-invitation]").last.click()
        assert page.locator(".invitation-variant").count() == 4
        page.get_by_role("button", name="Сохранить кампанию", exact=True).click()
        page.wait_for_url("**campaign_saved=1**")
        page.reload(wait_until="networkidle")
        assert page.locator("#delivery-mode").input_value() == "chat_invite"
        assert (
            "Спасибо за активность"
            in page.locator('[name="public_reply_variants"]').first.input_value()
        )
        page.locator("#delivery-mode").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "invitations.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Invitations overflow on mobile"
        )
        page.locator("#invitation-variants").scroll_into_view_if_needed()
        page.screenshot(path=str(output / "invitations-mobile.png"))
        page.set_viewport_size({"width": 1536, "height": 1024})
        for section in ("chat", "settings", "stats", "clients"):
            page.goto("http://127.0.0.1:8765/admin?section=" + section)
            assert page.locator("h1").count() == 1
        # Chat-link settings use a real HTML form/CSRF token on the isolated server.
        page.goto("http://127.0.0.1:8765/admin?section=settings")
        custom_chat_url = "https://vk.me/sarkisian.brand"
        page.locator('[name="chat_url"]').fill(custom_chat_url)
        page.get_by_role("button", name="Сохранить настройки", exact=True).click()
        page.wait_for_url("**saved=1")
        assert page.locator('[name="chat_url"]').input_value() == custom_chat_url
        assert page.locator("#saved-chat-url").get_attribute("href") == custom_chat_url
        page.screenshot(path=str(output / "chat-url-settings.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(
            path=str(output / "chat-url-settings-mobile.png"), full_page=True
        )
        page.set_viewport_size({"width": 1536, "height": 1024})
        page.goto("http://127.0.0.1:8765/admin?section=campaigns")
        assert (
            page.locator(f'#invitation-settings a[href="{custom_chat_url}"]').count()
            == 1
        )
        page.locator("#delivery-mode").select_option("chat_invite")
        page.get_by_role("link", name="Изменить ссылку на чат →").click()
        page.wait_for_url("**section=settings#chat-link-settings")
        assert page.locator('[name="chat_url"]').input_value() == custom_chat_url
        page.locator('[name="chat_url"]').fill("")
        page.get_by_role("button", name="Сохранить настройки", exact=True).click()
        page.wait_for_url("**saved=1")
        assert (
            page.locator("#saved-chat-url").get_attribute("href")
            == "https://vk.me/club123"
        )
        page.goto("http://127.0.0.1:8765/admin?section=chat")
        page.locator('[name="operator_user_id"]').fill("99, 100\n99")
        page.get_by_role("button", name="Сохранить настройки", exact=True).click()
        page.wait_for_url("**saved=1")
        assert page.locator('[name="operator_user_id"]').input_value() == "99, 100"
        page.screenshot(path=str(output / "managers.png"), full_page=True)
        page.locator('[name="operator_user_id"]').fill("99, bad")
        page.get_by_role("button", name="Сохранить настройки", exact=True).click()
        page.wait_for_url("**settings_error=operators")
        page.locator("#page-notice").get_by_text(
            "Настройки не сохранены.", exact=False
        ).wait_for()
        assert page.locator('[name="operator_user_id"]').input_value() == "99, 100"
        # UI-only fixture: do not send anything to VK or modify real clients.
        page.route(
            "**/admin/api/conversations",
            lambda route: route.fulfill(
                json=[
                    {
                        "user_id": 77,
                        "name": "Анна",
                        "handoff": True,
                        "assigned_operator_id": 99,
                        "assigned_at": "2026-09-25 12:30:00",
                        "variables": {"size": "M"},
                        "events": [
                            {
                                "text": "Менеджер id99: Здравствуйте!",
                                "status": "done",
                                "kind": "operator_reply",
                                "date": "2026-09-25 12:30:00",
                                "error": "",
                            }
                        ],
                    }
                ]
            ),
        )
        page.goto("http://127.0.0.1:8765/admin?section=dialogs")
        page.get_by_text("В работе у менеджера", exact=True).wait_for()
        page.get_by_role("link", name="id99 ↗").wait_for()
        page.get_by_text("Ответ менеджера", exact=False).wait_for()
        page.screenshot(path=str(output / "assigned-client.png"), full_page=True)
        page.unroute("**/admin/api/conversations")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto("http://127.0.0.1:8765/admin?section=scenarios")
        page.locator('.flow-node[data-id="start"]').wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Mobile page overflows"
        )
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        browser.close()
    assert not errors, errors
    print(
        "Browser smoke passed: editor, save/publish, preview, validation, campaigns, managers, assigned client, sections, mobile."
    )


if __name__ == "__main__":
    main()
