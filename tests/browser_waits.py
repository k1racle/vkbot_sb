"""Scenario wait editor and virtual time, inside the isolated browser fixture."""

import re
from datetime import datetime, timedelta, timezone

from app import db, projects
from app.flows import Graph


def exercise_waits(page, output):
    initial = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "x": 50, "y": 50, "next": "before"},
                {
                    "id": "before",
                    "type": "message",
                    "text": "Мы напомним о себе позже",
                    "x": 380,
                    "y": 50,
                    "next": "wait",
                },
                {"id": "wait", "type": "wait", "x": 710, "y": 50, "next": "end"},
                {
                    "id": "end",
                    "type": "end",
                    "text": "Нужна помощь с выбором, {first_name}?",
                    "x": 710,
                    "y": 360,
                },
            ]
        }
    ).model_dump()
    ident = page.evaluate(
        """async graph => {
        const flow = await Admin.api('/scenarios', 'POST');
        await Admin.api(`/scenarios/${flow.id}`, 'PUT', {title: 'Пауза перед напоминанием', graph, revision: flow.revision});
        return flow.id;
    }""",
        initial,
    )
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    page.locator('[data-add="wait"]').click()
    inspector = page.locator("#block-inspector")
    assert inspector.locator('[data-field="delay_value"]').input_value() == "3"
    assert inspector.locator('[data-field="delay_unit"]').input_value() == "hours"
    inspector.locator("#delete-node").click()
    page.locator('.flow-node[data-id="wait"] .node-heading').click()
    inspector.locator('[data-field="delay_value"]').fill("90")
    inspector.locator('[data-field="delay_unit"]').select_option("minutes")
    page.locator("#save-flow").click()
    page.locator("#dirty-state").get_by_text(
        "Все изменения сохранены", exact=True
    ).wait_for()
    page.reload()
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    page.locator('.flow-node[data-id="wait"] .node-heading').click()
    assert inspector.locator('[data-field="delay_value"]').input_value() == "90"
    assert inspector.locator('[data-field="delay_unit"]').input_value() == "minutes"
    page.screenshot(path=str(output / "scenario-wait-desktop.png"), full_page=True)
    page.locator("#preview-flow").click()
    page.locator("#skip-preview-wait").wait_for()
    assert "5400 сек." in page.locator("#preview-buttons").inner_text()
    assert "Нужна помощь" not in page.locator("#preview-messages").inner_text()
    page.locator("#preview-text").fill("Ещё один вопрос")
    page.locator("#preview-form button").click()
    page.wait_for_function("!document.querySelector('#preview-form button').disabled")
    assert "Нужна помощь" not in page.locator("#preview-messages").inner_text()
    page.locator("#skip-preview-wait").click()
    page.locator("#preview-messages").get_by_text(
        "Нужна помощь с выбором, Анна?", exact=True
    ).wait_for()
    assert page.locator("#skip-preview-wait").count() == 0
    page.screenshot(path=str(output / "scenario-wait-preview.png"), full_page=True)
    page.locator("#close-preview").click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(output / "scenario-wait-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.set_viewport_size({"width": 1440, "height": 1000})
    # A local fixture timer exercises the visible status and cancel control.
    project_id = int(re.search(r"/p/(\d+)/", page.url)[1])
    project = projects.get_project(project_id)
    with projects.project_scope(project), db.SessionLocal() as session:
        session.add(db.Client(user_id=8877, first_name="Проверка таймера"))
        session.add(
            db.Conversation(
                user_id=8877,
                scenario_id=ident,
                node_id="wait",
                variables={
                    "first_name": "Проверка таймера",
                    "_wait_id": "browser-wait",
                },
            )
        )
        session.add(
            db.ScenarioWait(
                id="browser-wait",
                user_id=8877,
                active_user=8877,
                scenario_id=ident,
                version=0,
                node_id="wait",
                due_at=datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=3),
            )
        )
        session.commit()
    page.goto(f"http://127.0.0.1:8766/p/{project_id}/admin?section=chat")
    page.get_by_text("Ожидание до", exact=True).wait_for()
    page.screenshot(path=str(output / "scenario-wait-customer.png"), full_page=True)
    page.get_by_role("button", name="Отменить ожидание", exact=True).click()
    page.get_by_text("Ожидание отменено", exact=True).wait_for()
    with projects.project_scope(project), db.SessionLocal() as session:
        assert session.get(db.ScenarioWait, "browser-wait").status == "cancelled"
    page.goto(f"http://127.0.0.1:8766/p/{project_id}/admin?section=scenarios")
    page.locator(f'#flow-select option[value="{ident}"]').wait_for(state="attached")
    page.locator("#flow-select").select_option(str(ident))
    page.locator("#delete-flow").click()
    page.locator('.flow-node[data-id="welcome"]').wait_for()
