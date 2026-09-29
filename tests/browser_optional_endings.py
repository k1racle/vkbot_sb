"""Optional fallback and terminal exits through the real admin UI."""

from app.flows import Graph


def exercise_optional_endings(page, output):
    original_id = page.locator("#flow-select").input_value()
    editor_url = page.url
    data = Graph.model_validate(
        {
            "entry": {"mode": "keywords", "keywords": "проверка конца"},
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "Начало",
                    "next": "hello",
                    "x": 60,
                    "y": 80,
                },
                {
                    "id": "hello",
                    "type": "message",
                    "title": "Последнее сообщение",
                    "text": "Ответ без блока завершения",
                    "x": 390,
                    "y": 80,
                },
            ],
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
        const row = await Admin.api('/scenarios', 'POST');
        await Admin.api(`/scenarios/${row.id}`, 'PUT', {title: 'Без завершения', graph, revision: row.revision});
        return row.id;
    }""",
        data,
    )
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    page.locator('.flow-node[data-id="hello"] .node-heading').click()
    assert (
        page.locator('[data-field="next"] option:checked').inner_text()
        == "Закончить без сообщения"
    )
    assert (
        "конец" in page.locator('.flow-node[data-id="hello"] .node-output').inner_text()
    )
    assert page.locator('.flow-node[data-kind="end"]').count() == 0
    page.locator("#publish-flow").click()
    page.locator("#flow-status").get_by_text("Опубликован", exact=False).wait_for()
    page.locator("#preview-flow").click()
    page.locator("#preview-messages").get_by_text(
        "Ответ без блока завершения", exact=True
    ).wait_for()
    page.locator("#close-preview").click()
    page.screenshot(path=str(output / "optional-end-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-field="next"]').scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=str(output / "optional-end-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#delete-flow").click()
    page.wait_for_function(
        "id => document.querySelector('#flow-select').value === id", arg=original_id
    )

    page.goto("http://127.0.0.1:8766/p/1/admin?section=chat")
    greeting = page.locator('[name="chat_greeting"]')
    assert not greeting.evaluate("el => el.required")
    greeting.fill("")
    page.locator('[name="chat_enabled"]').check()
    page.locator(
        'form[action="/p/1/admin/chat-settings"] button[type="submit"]'
    ).click()
    page.wait_for_url("**/admin?section=chat&saved=1")
    page.reload()
    assert greeting.input_value() == ""
    assert page.locator('[name="chat_enabled"]').is_checked()
    assert (
        "Если подходящего сценария нет"
        in page.locator('form[action="/p/1/admin/chat-settings"]').inner_text()
    )
    page.screenshot(path=str(output / "empty-chat-fallback.png"), full_page=True)
    page.goto(editor_url)
    page.locator(f'#flow-select option[value="{original_id}"]').wait_for(
        state="attached"
    )
    page.locator("#flow-select").select_option(original_id)
