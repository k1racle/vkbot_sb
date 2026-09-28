"""Exercise new module forms and the simulator inside the isolated browser run."""

from app.flows import Graph


def exercise_modules(page, output):
    initial = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "x": 50, "y": 50, "next": "phone"},
                {
                    "id": "phone",
                    "type": "contact",
                    "x": 380,
                    "y": 50,
                    "variable": "phone",
                    "text": "Ваш телефон?",
                    "next": "email",
                },
                {
                    "id": "email",
                    "type": "contact",
                    "x": 710,
                    "y": 50,
                    "contact_type": "email",
                    "variable": "email",
                    "text": "Ваш email?",
                    "next": "save",
                },
                {
                    "id": "save",
                    "type": "set_variable",
                    "x": 50,
                    "y": 330,
                    "variable": "request",
                    "value": "Запрос: {email}",
                    "next": "check",
                },
                {
                    "id": "check",
                    "type": "variable_condition",
                    "x": 380,
                    "y": 330,
                    "variable": "phone",
                    "comparison": "not_empty",
                    "yes": "random",
                    "no": "end",
                },
                {
                    "id": "random",
                    "type": "random",
                    "x": 710,
                    "y": 330,
                    "variants": [
                        "Спасибо, {first_name}!",
                        "Рады помочь, {first_name}!",
                    ],
                    "next": "end",
                },
                {"id": "end", "type": "end", "x": 380, "y": 620, "text": "Готово"},
            ]
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
      const flow = await Admin.api('/scenarios', 'POST');
      await Admin.api(`/scenarios/${flow.id}`, 'PUT', { title: 'Новые модули', graph, revision: flow.revision });
      return flow.id;
    }""",
        initial,
    )
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    for kind in ("contact", "set_variable", "variable_condition", "random"):
        assert page.locator(f'[data-add="{kind}"]').is_visible()
    # Create through the palette too, then delete the disconnected scratch node.
    page.locator('[data-add="contact"]').click()
    inspector = page.locator("#block-inspector")
    inspector.locator('[data-field="contact_type"]').select_option("email")
    assert inspector.locator('[data-field="variable"]').input_value() == "email"
    assert "name@example.com" in inspector.locator('[data-field="text"]').input_value()
    inspector.locator("#delete-node").click()
    page.locator('.flow-node[data-id="phone"] .node-heading').click()
    inspector.locator('[data-field="allow_skip"]').uncheck()
    inspector.locator('[data-field="error_text"]').fill("Введите корректный телефон")
    page.locator('.flow-node[data-id="save"] .node-heading').click()
    inspector.locator('[data-field="value"]').fill("Контакт: {email}")
    page.locator('.flow-node[data-id="check"] .node-heading').click()
    inspector.locator('[data-field="comparison"]').select_option("gte")
    inspector.locator('[data-field="value"]').fill("1500")
    inspector.locator('[data-field="comparison"]').select_option("not_empty")
    assert inspector.locator('[data-field="value"]').count() == 0
    page.locator('.flow-node[data-id="random"] .node-heading').click()
    inspector.locator("#add-variant").click()
    inspector.locator('[data-field="variants.2"]').fill("Заявка принята, {first_name}!")
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    saved = page.evaluate(
        "async id => (await Admin.api('/scenarios')).scenarios.find(s => s.id === id).graph",
        ident,
    )
    nodes = {n["id"]: n for n in saved["nodes"]}
    assert nodes["phone"]["allow_skip"] is False
    assert nodes["save"]["value"] == "Контакт: {email}"
    assert len(nodes["random"]["variants"]) == 3
    page.screenshot(path=str(output / "scenario-new-modules.png"), full_page=True)
    page.locator("#preview-flow").click()
    page.locator("#preview-dialog").wait_for(state="visible")
    page.locator("#preview-messages").get_by_text("Ваш телефон?", exact=True).wait_for()
    for answer, expected in [
        ("не номер", "Введите корректный телефон"),
        ("+7 999 123-45-67", "Ваш email?"),
        ("anna@example.com", "Готово"),
    ]:
        page.locator("#preview-text").fill(answer)
        page.locator("#preview-form button").click()
        page.locator("#preview-messages").get_by_text(expected, exact=False).wait_for()
    page.screenshot(path=str(output / "scenario-modules-preview.png"), full_page=True)
    page.locator("#close-preview").click()
    # Leave the original starter scenario intact for the rest of the browser run.
    page.locator("#delete-flow").click()
    page.locator('.flow-node[data-id="welcome"]').wait_for()
