"""Contact settings, real simulator buttons, and the left block library."""

from app.flows import Graph


def exercise_contact_reminders(page, output):
    data = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "next": "phone", "x": 20, "y": 80},
                {
                    "id": "phone",
                    "type": "contact",
                    "text": "Ваш телефон?",
                    "variable": "phone",
                    "next": "end",
                    "x": 320,
                    "y": 80,
                },
                {
                    "id": "end",
                    "type": "end",
                    "text": "Спасибо, телефон: {phone}",
                    "x": 620,
                    "y": 80,
                },
            ]
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
        const row = await Admin.api('/scenarios', 'POST');
        await Admin.api(`/scenarios/${row.id}`, 'PUT', {title: 'Сбор телефона с напоминанием', graph, revision: row.revision});
        return row.id;
    }""",
        data,
    )

    def select_flow():
        page.reload()
        page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
        page.locator("#flow-select").select_option(str(ident))
        page.locator('.flow-node[data-id="phone"] .node-heading').click()

    select_flow()
    library, canvas = (
        page.locator("#block-library").bounding_box(),
        page.locator("#flow-canvas").bounding_box(),
    )
    assert library["x"] + library["width"] <= canvas["x"] + 1
    assert abs(library["y"] - canvas["y"]) < 2
    page.locator("#toggle-block-library").click()
    assert page.locator("#flow-canvas").bounding_box()["width"] > canvas["width"]
    page.locator("#toggle-block-library").click()
    page.locator('[data-field="allow_skip"]').uncheck()
    page.locator('[data-field="reminder_enabled"]').check()
    page.locator('[data-field="reminder_delay_value"]').fill("10")
    page.locator('[data-field="reminder_delay_unit"]').select_option("minutes")
    page.locator('[data-field="reminder_text"]').fill("Ждём ваш номер, {first_name}")
    page.locator('[data-field="allow_later"]').check()
    page.locator('[data-field="later_text"]').fill("Хорошо, вернитесь, когда удобно")
    page.locator('[data-field="later_reminder_enabled"]').check()
    page.locator('[data-field="later_reminder_delay_value"]').fill("30")
    page.locator('[data-field="later_reminder_delay_unit"]').select_option("minutes")
    page.locator('[data-field="later_reminder_text"]').fill("Обещанное напоминание")
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    select_flow()
    assert page.locator('[data-field="reminder_delay_value"]').input_value() == "10"
    assert (
        page.locator('[data-field="later_reminder_delay_value"]').input_value() == "30"
    )
    page.locator("#fullscreen-flow").click()
    page.locator('[data-field="reminder_enabled"]').scroll_into_view_if_needed()
    page.screenshot(path=str(output / "contact-library-desktop.png"), full_page=True)
    page.locator("#preview-flow").click()
    page.locator("#skip-preview-wait").get_by_text(
        "Отправить напоминание сейчас", exact=True
    ).wait_for()
    page.locator("#preview-text").fill("123")
    page.locator("#preview-form button").click()
    page.locator("#preview-messages").get_by_text(
        "Введите телефон:", exact=False
    ).wait_for()
    assert page.locator("#skip-preview-wait").count() == 1
    page.locator("#preview-buttons button").get_by_text("Позже", exact=True).click()
    page.locator("#preview-messages").get_by_text(
        "Хорошо, вернитесь, когда удобно", exact=True
    ).wait_for()
    page.locator("#preview-buttons .hint").get_by_text(
        "Напоминание через 1800 сек.", exact=False
    ).wait_for()
    page.locator("#skip-preview-wait").click()
    page.locator("#preview-messages").get_by_text(
        "Обещанное напоминание", exact=True
    ).wait_for()
    assert page.locator("#skip-preview-wait").count() == 0
    assert (
        page.locator("#preview-buttons button")
        .get_by_text("Позже", exact=True)
        .is_visible()
    )
    page.locator("#preview-text").fill("89991234567")
    page.locator("#preview-form button").click()
    page.locator("#preview-messages").get_by_text(
        "Спасибо, телефон: +79991234567", exact=True
    ).wait_for()
    assert page.locator("#preview-buttons button").count() == 0
    page.locator("#restart-preview").click()
    page.locator("#skip-preview-wait").wait_for()
    page.locator("#preview-text").fill("89991234567")
    page.locator("#preview-form button").click()
    page.locator("#preview-messages").get_by_text(
        "Спасибо, телефон: +79991234567", exact=True
    ).wait_for()
    assert page.locator("#skip-preview-wait").count() == 0
    page.locator("#close-preview").click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#block-library-content").wait_for(state="hidden")
    page.locator("#toggle-block-library").click()
    assert page.locator('[data-add="contact"]').is_visible()
    page.screenshot(path=str(output / "contact-library-mobile.png"), full_page=True)
    page.locator('[data-add="phone_condition"]').click()
    assert page.locator("#block-library-content").is_hidden()
    page.locator("#delete-node").click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert page.locator("#flow-canvas").evaluate("el => el.clientHeight >= 80")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#fullscreen-flow").click()
    page.locator("#delete-flow").click()
    page.locator('.flow-node[data-id="welcome"]').wait_for()
