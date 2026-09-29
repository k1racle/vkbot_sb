"""New blocks and distraction-free editing in an isolated, no-VK browser run."""

from app.flows import Graph


def exercise_automation(page, output):
    data = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "next": "hours", "x": 30, "y": 30},
                {
                    "id": "hours",
                    "type": "schedule",
                    "title": "Рабочие часы",
                    "yes": "tag",
                    "no": "closed",
                    "x": 350,
                    "y": 30,
                },
                {
                    "id": "tag",
                    "type": "tag",
                    "tag": "интерес",
                    "next": "call",
                    "x": 670,
                    "y": 30,
                },
                {
                    "id": "call",
                    "type": "call_subflow",
                    "title": "Уточнить город",
                    "subflow_id": "delivery",
                    "return_variables": ["city"],
                    "next": "done",
                    "x": 350,
                    "y": 310,
                },
                {
                    "id": "done",
                    "type": "end",
                    "text": "Готово: {city}",
                    "x": 670,
                    "y": 310,
                },
                {
                    "id": "closed",
                    "type": "end",
                    "text": "Нерабочее время",
                    "x": 30,
                    "y": 310,
                },
                {
                    "id": "delivery",
                    "type": "subflow",
                    "title": "Доставка",
                    "next": "ask",
                    "x": 30,
                    "y": 620,
                },
                {
                    "id": "ask",
                    "type": "wait_reply",
                    "title": "Уточнить адрес",
                    "text": "Ваш город?",
                    "variable": "city",
                    "yes": "back",
                    "no": "missed",
                    "x": 350,
                    "y": 620,
                },
                {
                    "id": "missed",
                    "type": "tag",
                    "tag": "нет ответа",
                    "next": "back",
                    "x": 670,
                    "y": 620,
                },
                {"id": "back", "type": "return", "x": 670, "y": 900},
            ]
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
        const row = await Admin.api('/scenarios', 'POST');
        await Admin.api(`/scenarios/${row.id}`, 'PUT', {title: 'Расписание и доставка', graph, revision: row.revision});
        return row.id;
    }""",
        data,
    )
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    for kind in (
        "wait_reply",
        "schedule",
        "tag",
        "tag_condition",
        "subflow",
        "call_subflow",
        "return",
    ):
        page.locator(f'[data-add="{kind}"]').click()
        assert page.locator("#block-inspector h3").is_visible()
        page.locator("#delete-node").click()
    page.locator('.flow-node[data-id="hours"] .node-heading').click()
    inspector = page.locator("#block-inspector")
    inspector.locator('[data-weekday="5"]').check()
    inspector.locator('[data-field="time_to"]').fill("19:00")
    inspector.locator('[data-field="title"]').fill("Расписание магазина")
    page.locator("#fullscreen-flow").click()
    assert page.locator("#flow-app").evaluate(
        "el => el.classList.contains('is-fullscreen')"
    )
    assert not page.locator(".sidebar").is_visible()
    assert page.locator("#flow-app").evaluate(
        "el => Math.abs(el.getBoundingClientRect().width - innerWidth) < 2"
    )
    assert page.locator("#flow-canvas").evaluate("el => el.clientHeight > 150")
    page.screenshot(
        path=str(output / "automation-fullscreen-desktop.png"), full_page=True
    )
    page.keyboard.press("Escape")
    assert page.locator(".sidebar").is_visible()
    assert (
        inspector.locator('[data-field="title"]').input_value() == "Расписание магазина"
    )
    assert "Есть изменения" in page.locator("#dirty-state").inner_text()
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    page.locator("#fullscreen-flow").click()
    page.locator("#preview-flow").click()
    page.locator("#preview-clock").fill("2026-09-29T22:00")
    page.locator("#restart-preview").click()
    page.locator("#preview-messages").get_by_text(
        "Нерабочее время", exact=True
    ).wait_for()
    page.locator("#preview-clock").fill("2026-09-29T09:00")
    page.locator("#restart-preview").click()
    page.locator("#preview-messages").get_by_text("Ваш город?", exact=True).wait_for()
    assert "Вложенность: 1" in page.locator("#preview-state").inner_text()
    page.locator("#preview-text").fill("Москва")
    page.locator("#preview-form button").click()
    page.locator("#preview-messages").get_by_text(
        "Готово: Москва", exact=True
    ).wait_for()
    page.locator("#restart-preview").click()
    page.locator("#skip-preview-wait").wait_for()
    assert "время вышло" in page.locator("#skip-preview-wait").inner_text()
    page.locator("#skip-preview-wait").click()
    page.locator("#preview-state").get_by_text(
        "Метки: интерес, нет ответа · Вложенность: 0", exact=True
    ).wait_for()
    page.screenshot(path=str(output / "automation-preview.png"), full_page=True)
    page.keyboard.press("Escape")
    assert page.locator("#flow-app").evaluate(
        "el => el.classList.contains('is-fullscreen')"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator("#fullscreen-flow").is_visible()
    assert page.locator("#flow-canvas").evaluate("el => el.clientHeight >= 80")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(
        path=str(output / "automation-fullscreen-mobile.png"), full_page=True
    )
    page.locator("#fullscreen-flow").click()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#delete-flow").click()
    page.locator('.flow-node[data-id="welcome"]').wait_for()
