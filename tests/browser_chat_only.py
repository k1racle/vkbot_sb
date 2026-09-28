"""Exercise no-gift campaign editing in an isolated second project."""


def exercise_chat_only(page, output):
    origin = "http://127.0.0.1:8766"
    page.goto(origin + "/p/2/admin?section=campaigns&new_campaign=1")
    form = page.locator('form[action="/p/2/admin/campaigns"]')
    mode = form.locator('[name="delivery_mode"]')
    mode.select_option("chat_only")
    assert not form.locator("#campaign-promo-fields").is_visible()
    assert form.locator('[name="promo_code"]').is_disabled()
    assert (
        form.locator("#campaign-repeat-label").inner_text()
        == "Одно приглашение клиенту в этой кампании"
    )
    invitations = form.locator('[name="public_reply_variants"]')
    assert invitations.count() == 3
    assert all(
        "подар" not in value.casefold()
        for value in invitations.evaluate_all("els => els.map(el => el.value)")
    )
    invitations.first.fill("Спасибо! Подберём товар в чате: {chat_url}")
    mode.select_option("chat_invite")
    assert form.locator('[name="promo_code"]').is_enabled()
    assert form.locator("#campaign-promo-fields").is_visible()
    assert "подарок" in invitations.first.input_value()
    invitations.first.fill("Подарок здесь: {chat_url}")
    mode.select_option("chat_only")
    assert (
        invitations.first.input_value() == "Спасибо! Подберём товар в чате: {chat_url}"
    )
    mode.select_option("chat_invite")
    assert invitations.first.input_value() == "Подарок здесь: {chat_url}"
    mode.select_option("chat_only")
    form.locator('[name="title"]').fill("Подбор товара в чате")
    form.locator('[name="plus_words"]').fill("подбор, помогите")
    form.locator('button[type="submit"]').click()
    page.wait_for_url("**/*campaign_saved=1*")
    assert page.locator('[name="delivery_mode"]').input_value() == "chat_only"
    assert (
        page.locator('[name="public_reply_variants"]').first.input_value()
        == "Спасибо! Подберём товар в чате: {chat_url}"
    )
    assert page.locator('[name="promo_message"]').is_disabled()
    assert "Комментари" in page.content()
    assert "Сценарий начнётся только после действия получателя" in page.content()
    page.locator("h1").click()
    page.screenshot(path=str(output / "chat-only-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=str(output / "chat-only-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
