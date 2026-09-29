"""Phone condition editor and simulator, against browser_projects' isolated DB."""

from app.flows import Graph


def exercise_phone(page, output):
    data = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "next": "check", "x": 40, "y": 80},
                {
                    "id": "check",
                    "type": "phone_condition",
                    "title": "Клиент оставил телефон?",
                    "yes": "done",
                    "no": "ask",
                    "x": 370,
                    "y": 80,
                },
                {
                    "id": "ask",
                    "type": "contact",
                    "variable": "customer_phone",
                    "text": "Как с вами связаться?",
                    "next": "done",
                    "x": 370,
                    "y": 390,
                },
                {
                    "id": "done",
                    "type": "end",
                    "text": "Продолжаем без повторного запроса",
                    "x": 710,
                    "y": 80,
                },
            ]
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
        const row = await Admin.api('/scenarios', 'POST');
        await Admin.api(`/scenarios/${row.id}`, 'PUT', {title: 'Проверка телефона', graph, revision: row.revision});
        return row.id;
    }""",
        data,
    )

    def select_flow():
        page.reload()
        page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
        page.locator("#flow-select").select_option(str(ident))
        page.locator('.flow-node[data-id="check"] .node-heading').click()

    select_flow()
    page.locator('[data-add="phone_condition"]').click()
    assert "Телефон указан?" in page.locator("#block-inspector h3").inner_text()
    assert page.locator('[data-field="yes"]').count() == 1
    assert page.locator('[data-field="no"]').count() == 1
    assert page.locator('[data-field="phone_check_mode"]').input_value() == "provided"
    page.locator("#delete-node").click()
    page.locator('.flow-node[data-id="check"] .node-heading').click()
    page.locator('[data-field="phone_check_mode"]').select_option("any")
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    select_flow()
    assert page.locator('[data-field="phone_check_mode"]').input_value() == "any"
    page.locator("#fullscreen-flow").click()
    page.screenshot(path=str(output / "phone-condition-desktop.png"), full_page=True)
    page.locator("#preview-flow").click()
    page.locator("#preview-messages").get_by_text(
        "Как с вами связаться?", exact=False
    ).wait_for()
    page.locator("#preview-text").fill("89991234567")
    page.locator("#preview-form button").click()
    page.locator("#preview-contact-state").get_by_text(
        "Телефон сейчас: оставлен боту", exact=True
    ).wait_for()
    page.locator("#preview-messages").get_by_text(
        "Продолжаем без повторного запроса", exact=True
    ).wait_for()
    for preset in ("provided", "profile"):
        page.locator("#preview-phone").select_option(preset)
        page.locator("#restart-preview").click()
        page.locator("#preview-messages").get_by_text(
            "Продолжаем без повторного запроса", exact=True
        ).wait_for()
        assert (
            "Как с вами связаться?"
            not in page.locator("#preview-messages").inner_text()
        )
    page.locator("#close-preview").click()
    page.locator('[data-field="phone_check_mode"]').select_option("provided")
    page.locator("#preview-flow").click()
    page.locator("#preview-messages").get_by_text(
        "Как с вами связаться?", exact=False
    ).wait_for()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator("#preview-phone").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.locator("#preview-form").evaluate(
        "el => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }"
    )
    page.screenshot(path=str(output / "phone-condition-mobile.png"), full_page=True)
    page.locator("#preview-text").fill("89991234567")
    page.locator("#preview-form button").click()
    page.locator("#preview-contact-state").get_by_text(
        "Телефон сейчас: оставлен боту", exact=True
    ).wait_for()
    page.locator("#preview-phone").select_option("missing")
    page.locator("#close-preview").click()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#fullscreen-flow").click()
    page.locator("#delete-flow").click()
    page.locator('.flow-node[data-id="welcome"]').wait_for()
