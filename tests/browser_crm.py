"""UI-only CRM fixtures on a local test server; never sends to VK."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    output = Path("data/review")
    output.mkdir(parents=True, exist_ok=True)
    errors, actions = [], []
    people = [
        {
            "user_id": 77,
            "first_name": "Анна",
            "last_name": "Иванова",
            "photo_url": "",
            "vk_url": "https://vk.ru/id77",
            "phone": "+7 (000) 000-00-00",
            "phone_source": "dialog",
            "bot_contacted_at": "2026-09-25 10:00:00",
            "profile_updated_at": "2026-09-25 10:00:00",
            "profile_error": "",
            "unsubscribed": False,
            "deactivated": False,
            "messages_allowed": True,
        },
        {
            "user_id": 88,
            "first_name": "Мария",
            "last_name": "Петрова",
            "photo_url": "",
            "vk_url": "https://vk.ru/id88",
            "phone": "",
            "phone_source": "",
            "bot_contacted_at": "2026-09-24 12:30:00",
            "profile_updated_at": None,
            "profile_error": "",
            "unsubscribed": True,
            "deactivated": False,
            "messages_allowed": None,
        },
    ]
    jobs = []

    def mock(route):
        url = urlparse(route.request.url)
        path = url.path.removeprefix("/admin/api")
        method = route.request.method
        body = (
            route.request.post_data_json
            if method == "POST"
            and route.request.headers.get("content-type", "").startswith(
                "application/json"
            )
            else {}
        )
        if method == "POST":
            actions.append(path)
        result = {}
        if path == "/clients":
            query = parse_qs(url.query).get("q", [""])[0]
            rows = [c for c in people if not query or query in c["first_name"]]
            result = {
                "items": rows,
                "total": len(rows),
                "page": 1,
                "pages": 1,
                "profiles_pending": 0,
            }
        elif path == "/clients/refresh":
            result = {"queued": 2}
        elif path == "/clients/77":
            result = {
                "client": people[0],
                "handoff": False,
                "assigned_operator_id": None,
                "events": [
                    {
                        "text": "Здравствуйте! Хочу узнать о новинках.",
                        "date": "2026-09-25 10:00:00",
                        "status": "done",
                    }
                ],
                "deliveries": [],
                "variables": {},
            }
        elif path == "/media":
            result = {"id": "asset", "filename": "gift.txt"}
        elif path == "/broadcasts" and method == "GET":
            result = {"items": jobs, "eligible": 1}
        elif path == "/broadcasts" and method == "POST":
            job = {
                "id": "fixture",
                "title": body["title"],
                "message": body["message"],
                "media_id": body["media_id"],
                "filename": "gift.txt" if body["media_id"] else "",
                "total": 1,
                "counts": {"pending": 1},
                "status": "draft",
                "error": "",
                "created_at": "2026-09-25 12:00:00",
                "sample": [people[0]],
                "preview": body["message"].replace("{first_name}", "Анна")
                + "\n\nЧтобы отказаться от рассылок, напишите «Стоп».",
            }
            jobs.append(job)
            result = job
        elif path.endswith("/test"):
            result = {"ok": True}
        elif path.endswith("/start"):
            assert body["confirm_consent"] and body["expected_count"] == 1
            jobs[0]["status"] = "queued"
            result = jobs[0]
        elif path.endswith("/pause"):
            jobs[0]["status"] = "paused"
            result = jobs[0]
        elif path.endswith("/cancel"):
            jobs[0]["status"] = "cancelled"
            result = jobs[0]
        elif path == "/broadcasts/fixture":
            result = {
                **jobs[0],
                "recipients": [{"user_id": 77, "status": "pending", "error": ""}],
                "page": 1,
            }
        else:
            raise AssertionError(f"Unexpected CRM request: {method} {path}")
        route.fulfill(json=result)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1536, "height": 1024})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto("http://127.0.0.1:8765/login")
        page.locator('[name="username"]').fill("preview")
        page.locator('[name="password"]').fill("preview-local-only")
        page.locator('button[type="submit"]').click()
        page.wait_for_url("**/admin")
        page.route("**/admin/api/**", mock)
        page.goto("http://127.0.0.1:8765/admin?section=clients")
        expect(page.locator("#crm-total")).to_have_text("2")
        page.screenshot(path=str(output / "clients-desktop.png"), full_page=True)
        page.locator('[data-client="77"]').click()
        expect(page.locator("#crm-detail")).to_be_visible()
        expect(page.locator("#crm-detail-body")).to_contain_text(
            "Хочу узнать о новинках"
        )
        page.locator("#crm-detail [data-close-dialog]").click()
        page.locator("#crm-query").fill("Анна")
        page.locator("#crm-search button").click()
        expect(page.locator("#crm-total")).to_have_text("1")
        page.locator('[data-select-client="77"]').check()
        page.locator("#crm-mail-selected").click()
        page.wait_for_url("**section=broadcasts&audience=selected")
        expect(page.locator("#broadcast-audience")).to_have_value("selected")
        expect(page.locator("#broadcast-audience-note")).to_contain_text("1")
        page.locator("#broadcast-title").fill("Осенняя коллекция")
        page.locator("#broadcast-message").fill(
            "Привет, {first_name}!\n\nНовая коллекция уже в магазине. Приходи выбирать любимые образы — мы приготовили небольшой подарок."
        )
        page.locator("#broadcast-file").set_input_files(
            {"name": "gift.txt", "mimeType": "text/plain", "buffer": b"Test attachment"}
        )
        expect(page.locator("#broadcast-file-note")).to_contain_text(
            "Прикреплён: gift.txt"
        )
        page.screenshot(path=str(output / "broadcasts-desktop.png"), full_page=True)
        page.locator("#broadcast-prepare").click()
        expect(page.locator("#broadcast-preview")).to_be_visible()
        expect(page.locator("#broadcast-preview-text")).to_contain_text("Привет, Анна!")
        expect(page.locator("#broadcast-start")).to_be_disabled()
        assert not any(path.endswith("/start") for path in actions)
        page.locator("#broadcast-test-user").fill("77")
        page.locator("#broadcast-test").click()
        expect(page.locator("#broadcast-test-result")).to_contain_text("Тест отправлен")
        assert not any(path.endswith("/start") for path in actions)
        page.screenshot(path=str(output / "broadcast-preview.png"), full_page=True)
        page.locator("#broadcast-consent").check()
        page.locator("#broadcast-start").click()
        expect(page.locator("#broadcast-preview")).not_to_be_visible()
        expect(page.locator("#broadcast-jobs")).to_contain_text("В очереди")
        assert actions.count("/broadcasts/fixture/start") == 1
        page.locator('[data-action="pause"]').click()
        expect(page.locator("#broadcast-jobs")).to_contain_text("На паузе")
        page.locator('[data-action="log"]').click()
        expect(page.locator("#broadcast-log-body")).to_contain_text("77")
        page.locator("#broadcast-log [data-close-dialog]").click()
        page.locator('[data-action="cancel"]').click()
        expect(page.locator("#broadcast-jobs")).to_contain_text("Отменена")
        page.set_viewport_size({"width": 390, "height": 844})
        for section in ("broadcasts", "clients"):
            page.goto("http://127.0.0.1:8765/admin?section=" + section)
            target = "#broadcast-jobs" if section == "broadcasts" else "#crm-total"
            expect(page.locator(target)).not_to_have_text("Загрузка…")
            page.screenshot(
                path=str(output / (section + "-mobile.png")), full_page=True
            )
            assert page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            ), (
                section,
                page.evaluate(
                    "Array.from(document.querySelectorAll('*')).filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.className,e.id,e.getBoundingClientRect().right]).slice(0,20)"
                ),
            )
        browser.close()
    assert not errors, errors
    print(
        "CRM browser passed: clients, search, detail, selection, upload, preview, test, explicit consent/start, pause/cancel, log, mobile. No VK sends."
    )


if __name__ == "__main__":
    main()
