"""Launch settings persist and coexist with the default scenario in the UI."""


def exercise_scenario_triggers(page, output):
    default_id = page.locator("#flow-select").input_value()
    page.locator("#publish-flow").click()
    page.locator("#flow-status").get_by_text("Опубликован", exact=False).wait_for()
    page.locator("#new-flow").click()
    page.locator('[data-entry="mode"]').wait_for()
    page.wait_for_function(
        "id => document.querySelector('#flow-select').value !== id", arg=default_id
    )
    ident = page.locator("#flow-select").input_value()
    page.locator("#scenario-title").fill("Курс по ключевой фразе")
    page.locator('[data-entry="mode"]').select_option("keywords")
    page.locator("#publish-flow").click()
    page.locator("#flow-errors").get_by_text(
        "добавьте ключевые", exact=False
    ).wait_for()
    page.locator('[data-entry="keywords"]').fill("хочу курс\nзаписаться на курс")
    page.locator('[data-entry="match"]').select_option("exact")
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(ident)
    assert page.locator('[data-entry="mode"]').input_value() == "keywords"
    assert page.locator('[data-entry="match"]').input_value() == "exact"
    assert (
        page.locator('[data-entry="keywords"]').input_value()
        == "хочу курс\nзаписаться на курс"
    )
    page.locator("#publish-flow").click()
    page.locator("#flow-status").get_by_text("Опубликован", exact=False).wait_for()
    active = page.evaluate(
        "async () => (await Admin.api('/scenarios')).scenarios.filter(s => s.active).map(s => s.id)"
    )
    assert int(default_id) in active and int(ident) in active
    assert (
        "по фразам"
        in page.locator(f'#flow-select option[value="{ident}"]').inner_text()
    )
    assert (
        "по умолчанию"
        in page.locator(f'#flow-select option[value="{default_id}"]').inner_text()
    )
    page.locator("#fullscreen-flow").click()
    page.locator('[data-entry="mode"]').scroll_into_view_if_needed()
    page.screenshot(path=str(output / "scenario-keywords-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-entry="keywords"]').scroll_into_view_if_needed()
    assert page.locator('[data-entry="keywords"]').is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=str(output / "scenario-keywords-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#fullscreen-flow").click()
    page.locator("#delete-flow").click()
    page.wait_for_function(
        "id => document.querySelector('#flow-select').value === id", arg=default_id
    )
