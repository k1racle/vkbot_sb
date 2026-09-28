"""Isolated multi-project browser smoke; no real VK/network integrations."""

import os
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
import httpx
from urllib.parse import parse_qs
from playwright.sync_api import sync_playwright


def main():
    from app import config

    directory = Path(tempfile.mkdtemp(prefix="vk-project-browser-"))
    settings = config.Settings(
        _env_file=None,
        database_url=f"sqlite:///{(directory / 'legacy.db').as_posix()}",
        vk_group_id=123,
        vk_group_token="browser-dummy-token",
        vk_callback_secret="browser-dummy-secret",
        vk_confirmation_code="browser-dummy-code",
        admin_username="preview",
        admin_password="preview-only",
        admin_session_secret="browser-test-only-session-secret",
        projects_key_file=str(directory / "projects.key"),
        projects_encryption_key="",
        background_jobs_enabled=False,
    )
    config.get_base_settings = lambda: settings
    os.environ["PROJECTS_DATA_DIR"] = str(directory / "files")
    from app import main as application
    from app import vk_api

    async def no_vk(*args, **kwargs):
        raise AssertionError("Browser smoke must not contact VK")

    vk_api.call = no_vk
    vk_api._request = no_vk

    async def fake_group_info(_transport, request):
        assert str(request.url) == "https://api.vk.com/method/groups.getById"
        params = parse_qs((await request.aread()).decode())
        group_id = int(params.get("group_id", [123])[0])
        return httpx.Response(
            200,
            json={
                "response": {
                    "groups": [{"id": group_id, "name": f"Группа VK {group_id}"}]
                }
            },
            request=request,
        )

    original_transport = httpx.AsyncHTTPTransport.handle_async_request
    httpx.AsyncHTTPTransport.handle_async_request = fake_group_info
    server = uvicorn.Server(
        uvicorn.Config(
            application.app, host="127.0.0.1", port=8766, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    output = Path("data/review_projects")
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        assert server.started, "Review server did not start"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto("http://127.0.0.1:8766/login")
            assert page.locator("img.brand-mark").evaluate(
                "img => img.complete && img.naturalWidth > 0"
            )
            assert page.locator('link[rel="icon"]').get_attribute("href") == (
                "/static/vk-bot-logo-v1.png"
            )
            assert page.request.get(
                "http://127.0.0.1:8766/static/vk-bot-logo-v1.png"
            ).status == 200
            page.screenshot(path=str(output / "logo-login.png"), full_page=True)
            page.locator('[name="username"]').fill("preview")
            page.locator('[name="password"]').fill("preview-only")
            page.locator('button[type="submit"]').click()
            page.wait_for_url("**/projects")
            page.locator("#project-title-1").get_by_text(
                "Группа VK 123", exact=True
            ).wait_for()
            assert (
                page.locator('#project-select option[value="/p/1/admin"]').inner_text()
                == "Группа VK 123"
            )
            assert page.locator("img.brand-mark").evaluate(
                "img => img.complete && img.naturalWidth > 0"
            )
            assert "VK BOT" in page.locator(".brand").inner_text()
            form = page.locator('form[action="/projects"]')
            form.locator('[name="name"]').fill("Вторая группа")
            form.locator('[name="group_id"]').fill("456")
            form.locator('button[type="submit"]').click()
            page.locator("#project-2").wait_for()
            assert page.locator("#project-2").inner_text().find("На паузе") >= 0
            page.screenshot(path=str(output / "projects-desktop.png"), full_page=True)
            page.locator("#project-select").select_option("/p/1/admin")
            page.wait_for_url("**/p/1/admin")
            page.locator("#new-flow").click()
            page.locator('.flow-node[data-id="welcome"]').wait_for()
            from tests.browser_modules import exercise_modules

            exercise_modules(page, output)
            # The same browser cookie in another tab cannot switch this tab's API.
            other = context.new_page()
            other.on("pageerror", lambda error: errors.append(str(error)))
            other.goto("http://127.0.0.1:8766/p/2/admin")
            other.locator("#new-flow").wait_for()
            assert other.locator(".flow-node").count() == 0
            page.reload()
            page.locator('.flow-node[data-id="welcome"]').wait_for()
            assert page.locator("body").get_attribute("data-project-id") == "1"
            assert other.locator("body").get_attribute("data-project-id") == "2"
            other.locator('a[href="/p/2/admin?section=settings"]').click()
            other.locator('[name="chat_url"]').fill("https://vk.me/second-test")
            other.locator(
                'form[action="/p/2/admin/settings"] button[type="submit"]'
            ).click()
            other.wait_for_url("**/p/2/admin?section=settings&saved=1")
            assert (
                other.locator('[name="chat_url"]').input_value()
                == "https://vk.me/second-test"
            )
            page.goto("http://127.0.0.1:8766/p/1/admin?section=settings")
            assert page.locator('[name="chat_url"]').input_value() == ""
            other.screenshot(path=str(output / "project-settings.png"), full_page=True)
            # Save campaign keywords in the real UI, then download an XLSX.
            page.goto(
                "http://127.0.0.1:8766/p/1/admin?section=campaigns&new_campaign=true"
            )
            campaign = page.locator('form[action="/p/1/admin/campaigns"]')
            campaign.locator('[name="title"]').fill("Подарок за слово")
            campaign.locator('[name="promo_code"]').fill("TEST10")
            campaign.locator('[name="shop_url"]').fill("https://example.org")
            campaign.locator('[name="promo_message"]').fill("Ваш код {promo_code}")
            campaign.locator('[name="plus_words"]').fill("хочу\nподарок")
            campaign.locator('button[type="submit"]').click()
            page.wait_for_url("**/*campaign_saved=1*")
            assert page.locator('[name="plus_words"]').input_value() == "хочу\nподарок"
            from app import db, projects

            with (
                projects.project_scope(projects.get_project(1)),
                db.SessionLocal() as session,
            ):
                session.add(
                    db.Client(user_id=77, first_name="Анна", phone="+79990000000")
                )
                session.commit()
            page.goto("http://127.0.0.1:8766/p/1/admin?section=clients")
            page.locator('[data-client="77"]').wait_for()
            page.locator("#crm-query").fill("Анна")
            with page.expect_download() as download_info:
                page.locator("#crm-export").click()
            download = download_info.value
            assert download.suggested_filename == "clients-project-1.xlsx"
            download.save_as(output / "clients-export.xlsx")
            from openpyxl import load_workbook

            workbook = load_workbook(output / "clients-export.xlsx")
            assert workbook["Клиенты"]["B2"].value == "Анна"
            workbook.close()
            page.goto("http://127.0.0.1:8766/projects")
            page.locator("#project-1 .project-settings summary").first.click()
            settings_form = page.locator('form[action="/projects/1/settings"]')
            settings_form.locator('[name="video_token"]').fill("browser-video-secret")
            settings_form.locator('button[type="submit"]').click()
            page.wait_for_url("**/projects#project-1")
            assert "browser-video-secret" not in page.content()
            assert page.locator('[name="clear_video_token"]').count() == 1
            # Recoverable deletion from the real UI; no outgoing messages.
            page.goto("http://127.0.0.1:8766/p/1/admin?section=scenarios")
            page.locator('.flow-node[data-id="welcome"]').wait_for()
            assert page.locator(".node-symbol .icon").count() > 0
            icon_width = page.locator(".node-symbol .icon").first.evaluate(
                "el => parseFloat(getComputedStyle(el).width)"
            )
            assert 17 <= icon_width <= 19, icon_width
            assert page.locator(".side-nav .icon use").count() == 9
            assert page.locator(".side-nav .icon").first.evaluate(
                "el => el.getBBox().width > 0"
            )
            page.screenshot(path=str(output / "lucide-scenarios.png"), full_page=True)
            page.locator("#delete-flow").click()
            page.locator("#flow-empty").wait_for(state="visible")
            page.goto("http://127.0.0.1:8766/p/1/admin?section=trash")
            page.locator('[data-kind="scenario"][data-id="1"]').click()
            page.locator('[data-kind="scenario"][data-id="1"]').wait_for(
                state="detached"
            )
            page.goto("http://127.0.0.1:8766/p/1/admin?section=campaigns")
            page.get_by_role("button", name="Удалить кампанию", exact=True).click()
            page.wait_for_url("**/*campaign_deleted=1*")
            page.goto("http://127.0.0.1:8766/p/1/admin?section=trash")
            page.locator('[data-kind="campaign"]').click()
            page.locator('[data-kind="campaign"]').wait_for(state="detached")
            with (
                projects.project_scope(projects.get_project(1)),
                db.SessionLocal() as session,
            ):
                session.add(
                    db.Broadcast(
                        id="browser-mail",
                        title="Тест раасылки из админки бота",
                        message="Текст",
                        status="completed",
                    )
                )
                session.add(
                    db.BroadcastRecipient(
                        broadcast_id="browser-mail", user_id=77, status="sent"
                    )
                )
                session.commit()
            page.goto("http://127.0.0.1:8766/p/1/admin?section=broadcasts")
            page.locator('[data-action="rename"]').click()
            page.locator("#broadcast-rename-title").fill(
                "Тест рассылки из админки бота"
            )
            page.locator("#broadcast-rename-save").click()
            page.locator(".crm-job h3").get_by_text(
                "Тест рассылки из админки бота", exact=True
            ).wait_for()
            assert "1 получатель" in page.locator(".crm-job").inner_text()
            page.locator("h1").click()
            page.screenshot(path=str(output / "lucide-broadcasts.png"), full_page=True)
            page.locator('[data-action="delete"]').click()
            page.locator("#broadcast-jobs").get_by_text(
                "Рассылок ещё нет.", exact=False
            ).wait_for()
            page.goto("http://127.0.0.1:8766/p/1/admin?section=trash")
            page.locator('[data-kind="broadcast"]').click()
            page.locator('[data-kind="broadcast"]').wait_for(state="detached")
            page.goto("http://127.0.0.1:8766/projects")
            page.locator("#project-2").get_by_text("Удалить проект", exact=True).click()
            delete_form = page.locator('form[action="/projects/2/delete"]')
            delete_form.locator('[name="confirmation"]').fill("456")
            delete_form.locator('button[type="submit"]').click()
            page.locator('form[action="/projects/2/restore"] button').wait_for()
            assert (
                page.locator('#project-select option[value="/p/2/admin"]').count() == 0
            )
            page.locator('form[action="/projects/2/restore"] button').click()
            page.locator("#project-2").wait_for()
            assert not projects.get_project(2).enabled
            mobile = context.new_page()
            mobile.set_viewport_size({"width": 390, "height": 844})
            mobile.goto("http://127.0.0.1:8766/projects")
            assert mobile.locator("img.brand-mark").is_visible()
            assert mobile.locator("img.brand-mark").evaluate(
                "img => img.complete && img.naturalWidth > 0"
            )
            mobile.screenshot(path=str(output / "logo-mobile.png"), full_page=True)
            assert mobile.locator("#project-select").is_visible()
            assert mobile.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            )
            for section in ("campaigns", "clients", "broadcasts", "trash"):
                mobile.goto(f"http://127.0.0.1:8766/p/1/admin?section={section}")
                assert mobile.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth + 1"
                )
                mobile.screenshot(
                    path=str(output / f"{section}-mobile.png"), full_page=True
                )
            mobile.screenshot(path=str(output / "projects-mobile.png"), full_page=True)
            mobile.locator("#project-select").select_option("/p/2/admin")
            mobile.wait_for_url("**/p/2/admin")
            assert mobile.locator("#project-select").is_visible()
            assert mobile.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            )
            assert not errors, errors
            browser.close()
        print(
            "PASS: project names, new scenario modules, isolation, keywords, XLSX, masked tokens, trash/restore, rename, logo/favicon, Lucide icons, desktop/mobile, no JS errors"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        httpx.AsyncHTTPTransport.handle_async_request = original_transport


if __name__ == "__main__":
    main()
