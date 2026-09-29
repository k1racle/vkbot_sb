"""Validated graph and shared interpreter for VK and the admin simulator."""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from .flow_rules import (
    MAX_CALL_DEPTH,
    in_schedule,
    normalize_tag,
    schedule_errors,
    subflow_errors,
)

NodeId = str
KINDS = {
    "start",
    "message",
    "question",
    "condition",
    "promo",
    "operator",
    "end",
    "contact",
    "phone_condition",
    "set_variable",
    "variable_condition",
    "random",
    "wait",
    "wait_reply",
    "schedule",
    "tag",
    "tag_condition",
    "subflow",
    "call_subflow",
    "return",
}
WAIT_UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}
MAX_WAIT_SECONDS = 3650 * 86400
VARIABLE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_VARIABLES = {
    "first_name",
    "last_name",
    "user_name",
    "promo_code",
    "shop_url",
    "last_message",
}


class Button(BaseModel):
    label: str = Field(default="Кнопка", max_length=40)
    kind: Literal["next", "link"] = "next"
    target: str = Field(default="", max_length=64)
    url: str = Field(default="", max_length=1000)
    color: Literal["primary", "secondary", "positive", "negative"] = "primary"


class Rule(BaseModel):
    words: str = Field(default="", max_length=500)
    target: str = Field(default="", max_length=64)


class Node(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    type: Literal[
        "start",
        "message",
        "question",
        "condition",
        "promo",
        "operator",
        "end",
        "contact",
        "phone_condition",
        "set_variable",
        "variable_condition",
        "random",
        "wait",
        "wait_reply",
        "schedule",
        "tag",
        "tag_condition",
        "subflow",
        "call_subflow",
        "return",
    ]
    title: str = Field(default="Блок", max_length=120)
    x: float = Field(default=80, ge=0, le=10000, allow_inf_nan=False)
    y: float = Field(default=80, ge=0, le=10000, allow_inf_nan=False)
    text: str = Field(default="", max_length=3500)
    next: str = Field(default="", max_length=64)
    yes: str = Field(default="", max_length=64)
    no: str = Field(default="", max_length=64)
    buttons: list[Button] = Field(default_factory=list, max_length=5)
    rules: list[Rule] = Field(default_factory=list, max_length=10)
    variable: str = Field(default="answer", max_length=32)
    condition: Literal["contains", "member", "promo_sent"] = "contains"
    words: str = Field(default="", max_length=500)
    campaign_id: int | None = Field(default=None, gt=0)
    media_id: str = Field(default="", max_length=32)
    contact_type: Literal["phone", "email"] = "phone"
    phone_check_mode: Literal["provided", "any"] = "provided"
    allow_skip: bool = True
    error_text: str = Field(default="", max_length=500)
    reminder_enabled: bool = False
    reminder_delay_value: int = Field(default=3, ge=1, le=MAX_WAIT_SECONDS, strict=True)
    reminder_delay_unit: Literal["seconds", "minutes", "hours", "days"] = "hours"
    reminder_text: str = Field(default="", max_length=3500)
    allow_later: bool = False
    later_text: str = Field(default="", max_length=3500)
    later_reminder_enabled: bool = False
    later_reminder_delay_value: int = Field(
        default=24, ge=1, le=MAX_WAIT_SECONDS, strict=True
    )
    later_reminder_delay_unit: Literal["seconds", "minutes", "hours", "days"] = "hours"
    later_reminder_text: str = Field(default="", max_length=3500)
    value: str = Field(default="", max_length=1000)
    comparison: Literal[
        "equals",
        "not_equals",
        "contains",
        "empty",
        "not_empty",
        "gt",
        "gte",
        "lt",
        "lte",
    ] = "equals"
    variants: list[Annotated[str, Field(max_length=3500)]] = Field(
        default_factory=list, max_length=10
    )
    delay_value: int = Field(default=3, ge=1, le=MAX_WAIT_SECONDS, strict=True)
    delay_unit: Literal["seconds", "minutes", "hours", "days"] = "hours"
    tag: str = Field(default="", max_length=40)
    tag_action: Literal["add", "remove"] = "add"
    timezone: str = Field(default="Europe/Moscow", max_length=100)
    weekdays: list[Annotated[int, Field(ge=0, le=6, strict=True)]] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], max_length=7
    )
    time_from: str = Field(default="09:00", max_length=5)
    time_to: str = Field(default="18:00", max_length=5)
    date_from: str = Field(default="", max_length=10)
    date_to: str = Field(default="", max_length=10)
    subflow_id: str = Field(default="", max_length=64)
    pass_variables: list[Annotated[str, Field(max_length=32)]] = Field(
        default_factory=list, max_length=32
    )
    return_variables: list[Annotated[str, Field(max_length=32)]] = Field(
        default_factory=list, max_length=32
    )


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list, max_length=100)


def outputs(node):
    kind = node["type"]
    if kind == "wait_reply":
        return [("Ответил", node["yes"]), ("Время вышло", node["no"])]
    if kind == "call_subflow":
        return [("Подцепочка", node["subflow_id"]), ("После возврата", node["next"])]
    if kind in {
        "condition",
        "variable_condition",
        "schedule",
        "tag_condition",
        "phone_condition",
    }:
        return [("Да", node["yes"]), ("Нет", node["no"])]
    if kind in {"end", "operator", "return"}:
        return []
    if kind == "message" and node["buttons"]:
        return [
            (b["label"], b["target"]) for b in node["buttons"] if b["kind"] == "next"
        ]
    result = [("Далее" if kind != "question" else "Другой ответ", node["next"])]
    if kind == "question":
        result += [(r["words"], r["target"]) for r in node["rules"]]
    return result


def validate_graph(graph: dict, campaign_ids=(), media_ids=()) -> list[str]:
    errors = []
    nodes = graph["nodes"]
    by_id = {n["id"]: n for n in nodes}
    starts = [n for n in nodes if n["type"] == "start"]
    if len(starts) != 1:
        errors.append("Нужен ровно один блок «Старт».")
    if len(by_id) != len(nodes):
        errors.append("У блоков повторяются идентификаторы.")
    for n in nodes:
        prefix = n["title"] or n["id"]
        if n["type"] in {"wait", "wait_reply"} and wait_seconds(n) > MAX_WAIT_SECONDS:
            errors.append(f"{prefix}: ожидание не должно превышать 3650 дней (10 лет).")
        if n["type"] == "contact":
            for mode in ("silence", "later"):
                enabled, seconds, _ = contact_reminder(n, mode)
                if enabled and seconds > MAX_WAIT_SECONDS:
                    errors.append(
                        f"{prefix}: напоминание не должно быть позже 3650 дней (10 лет)."
                    )
        if (
            n["type"] in {"message", "question", "contact", "wait_reply"}
            and not n["text"].strip()
            and not n["media_id"]
        ):
            errors.append(f"{prefix}: добавьте текст или файл.")
        if n["type"] in {"question", "contact", "set_variable", "wait_reply"} and (
            not VARIABLE.fullmatch(n["variable"]) or n["variable"] in RESERVED_VARIABLES
        ):
            errors.append(
                f"{prefix}: имя переменной — латиница, цифры и _, например size; системные имена зарезервированы."
            )
        if n["type"] in {"tag", "tag_condition"}:
            try:
                normalize_tag(n["tag"])
            except ValueError as error:
                errors.append(f"{prefix}: {error}")
        if n["type"] == "schedule":
            errors.extend(f"{prefix}: {message}" for message in schedule_errors(n))
        if n["type"] == "call_subflow":
            for field in ("pass_variables", "return_variables"):
                if any(
                    not VARIABLE.fullmatch(v)
                    or (field == "return_variables" and v in RESERVED_VARIABLES)
                    for v in n[field]
                ):
                    errors.append(
                        f"{prefix}: проверьте имена передаваемых переменных; служебные значения возвращать нельзя."
                    )
        if n["type"] == "variable_condition":
            if not VARIABLE.fullmatch(n["variable"]):
                errors.append(
                    f"{prefix}: укажите имя проверяемой переменной, например size."
                )
            if n["comparison"] not in {"empty", "not_empty"} and not n["value"].strip():
                errors.append(f"{prefix}: укажите значение для сравнения.")
            if (
                n["comparison"] in {"gt", "gte", "lt", "lte"}
                and number(n["value"]) is None
            ):
                errors.append(
                    f"{prefix}: для числового сравнения укажите число, например 1500 или 1,5."
                )
        if n["type"] == "random" and (
            not 2 <= len(n["variants"]) <= 10
            or any(not v.strip() for v in n["variants"])
        ):
            errors.append(
                f"{prefix}: добавьте от 2 до 10 непустых вариантов сообщения."
            )
        if (
            n["type"] == "condition"
            and n["condition"] == "contains"
            and not n["words"].strip()
        ):
            errors.append(f"{prefix}: укажите слова для проверки.")
        if (
            n["type"] == "promo"
            or (n["type"] == "condition" and n["condition"] == "promo_sent")
        ) and n["campaign_id"] not in campaign_ids:
            errors.append(f"{prefix}: выберите существующую кампанию.")
        if n["media_id"] and n["media_id"] not in media_ids:
            errors.append(f"{prefix}: прикреплённый файл не найден.")
        for label, target in outputs(n):
            if target not in by_id:
                errors.append(f"{prefix} → {label}: выберите следующий блок.")
        for b in n["buttons"]:
            if not b["label"].strip():
                errors.append(f"{prefix}: подпишите кнопку.")
            if b["kind"] == "link" and (
                urlparse(b["url"]).scheme not in {"http", "https"}
                or not urlparse(b["url"]).netloc
            ):
                errors.append(
                    f"{prefix}: ссылка кнопки должна начинаться с https:// или http://."
                )
        if any(not r["words"].strip() for r in n["rules"]):
            errors.append(f"{prefix}: укажите слова в каждой ветке ответа.")
    if starts:
        seen = set()

        def visit(node_id):
            if node_id not in by_id or node_id in seen:
                return
            seen.add(node_id)
            for _, target in outputs(by_id[node_id]):
                visit(target)

        visit(starts[0]["id"])
        for n in nodes:
            if n["id"] not in seen:
                errors.append(f"{n['title']}: блок не соединён со стартом.")
    # A loop is safe only if it waits for a new user message.
    visiting, done = set(), set()

    def auto_cycle(node_id):
        if node_id in visiting:
            return True
        if node_id in done or node_id not in by_id:
            return False
        n = by_id[node_id]
        if n["type"] in {"question", "contact"} or (
            n["type"] == "message" and n["buttons"]
        ):
            return False
        visiting.add(node_id)
        edges = [("Время вышло", n["no"])] if n["type"] == "wait_reply" else outputs(n)
        cycle = any(auto_cycle(t) for _, t in edges)
        visiting.remove(node_id)
        done.add(node_id)
        return cycle

    if any(auto_cycle(n["id"]) for n in nodes):
        errors.append("Обнаружен бесконечный цикл без ожидания ответа клиента.")
    errors.extend(subflow_errors(nodes, outputs))
    return list(dict.fromkeys(errors))


def render(text, variables):
    return re.sub(
        r"\{([a-z][a-z0-9_]*)\}",
        lambda m: str(variables.get(m[1], m[0])),
        text.replace("\\n", "\n"),
    )[:4000]


def matches(text, words):
    return any(
        w.strip().casefold() in text.casefold()
        for w in words.replace(",", "\n").splitlines()
        if w.strip()
    )


def normalize_contact(text, kind):
    value = text.strip()
    if kind == "phone":
        if len(value) > 80 or not re.fullmatch(r"\+?[0-9\s().-]+", value):
            return None
        digits = re.sub(r"[^0-9]", "", value)
        if not 10 <= len(digits) <= 15:
            return None
        if len(digits) == 11 and digits.startswith("8") and not value.startswith("+"):
            return "+7" + digits[1:]
        return ("+" if value.startswith("+") else "") + digits
    if kind != "email" or len(value) > 254 or value.count("@") != 1:
        return None
    local, domain = value.rsplit("@", 1)
    if (
        not 1 <= len(local) <= 64
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return None
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = domain.split(".")
    if (
        len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in labels
        )
        or len(local) + len(domain) + 1 > 254
    ):
        return None
    return local + "@" + domain


def number(value):
    try:
        result = Decimal(str(value).strip().replace(",", "."))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def compare_variable(node, variables):
    raw = variables.get(node["variable"])
    value = "" if raw is None else str(raw).strip()
    expected = node["value"].strip()
    operation = node["comparison"]
    if operation == "empty":
        return not value
    if operation == "not_empty":
        return bool(value)
    if not value:
        return False
    if operation in {"equals", "not_equals", "contains"}:
        left, right = value.casefold(), expected.casefold()
        return {
            "equals": left == right,
            "not_equals": left != right,
            "contains": right in left,
        }[operation]
    left, right = number(value), number(expected)
    if left is None or right is None:
        return False
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[operation]


def keyboard_for(node, version, nonce):
    rows = []
    for i, b in enumerate(node["buttons"]):
        if b["kind"] == "link":
            rows.append(
                [
                    {
                        "action": {
                            "type": "open_link",
                            "label": b["label"],
                            "link": b["url"],
                        }
                    }
                ]
            )
        else:
            import json

            data = {"flow": version, "node": node["id"], "button": i, "nonce": nonce}
            rows.append(
                [
                    {
                        "action": {
                            "type": "text",
                            "label": b["label"],
                            "payload": json.dumps(data),
                        },
                        "color": b["color"],
                    }
                ]
            )
    return {"one_time": False, "buttons": rows}


def wait_seconds(node):
    return node["delay_value"] * WAIT_UNITS[node["delay_unit"]]


def contact_reminder(node, mode):
    """Defaults keep existing published graphs unchanged, including old JSON."""
    later = mode == "later"
    prefix = "later_reminder" if later else "reminder"
    enabled = bool(node.get(f"{prefix}_enabled", False))
    if later:
        enabled = enabled and node.get("allow_later", False)
    seconds = (
        node.get(f"{prefix}_delay_value", 24 if later else 3)
        * WAIT_UNITS[node.get(f"{prefix}_delay_unit", "hours")]
    )
    label = (
        "адрес электронной почты"
        if node["contact_type"] == "email"
        else "номер телефона"
    )
    message = node.get(f"{prefix}_text", "").strip() or (
        f"Напоминаем: пришлите {label}, когда вам будет удобно, чтобы продолжить."
    )
    return enabled, seconds, message


def contact_keyboard(node, state):
    import json

    if not node.get("allow_later", False):
        return {"one_time": False, "buttons": []}
    return {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "Позже",
                        "payload": json.dumps(
                            {
                                "flow": state["version"],
                                "node": node["id"],
                                "nonce": state.get("nonce", ""),
                                "contact_action": "later",
                            }
                        ),
                    },
                    "color": "secondary",
                }
            ]
        ],
    }


def arm_contact_reminder(node, state, mode):
    state.pop("waiting", None)
    state["variables"].pop("_contact_reminder", None)
    enabled, seconds, _ = contact_reminder(node, mode)
    if enabled:
        state["variables"]["_contact_reminder"] = {"node_id": node["id"], "mode": mode}
        state["waiting"] = {
            "node_id": node["id"],
            "seconds": seconds,
            "kind": "contact",
        }


async def accept_contact(node, state, text, payload, port, *, resume_wait=False):
    """Stay at the contact after reminders/later/errors; only valid/skip advances."""
    variables = state["variables"]
    keyboard = contact_keyboard(node, state)
    if resume_wait:
        pending = variables.pop("_contact_reminder", {})
        state.pop("waiting", None)
        if pending.get("node_id") == node["id"] and pending.get("mode") in {
            "silence",
            "later",
        }:
            enabled, _, message = contact_reminder(node, pending["mode"])
            if enabled:
                await port.emit(render(message, variables), keyboard=keyboard)
        return False
    if payload and not (
        node.get("allow_later", False)
        and payload.get("contact_action") == "later"
        and payload.get("node") == node["id"]
        and payload.get("flow") == state.get("version")
        and payload.get("nonce") == state.get("nonce")
    ):
        await port.emit(
            "Эта кнопка устарела. Пришлите контакт или напишите «меню».",
            keyboard=keyboard,
        )
        return False
    later = node.get("allow_later", False) and (
        payload or text.strip().casefold() == "позже"
    )
    if later:
        await port.cancel_contact_wait(state, "Клиент выбрал Позже")
        arm_contact_reminder(node, state, "later")
        message = (
            node.get("later_text", "").strip()
            or "Хорошо! Пришлите контакт, когда вам будет удобно. Я продолжу с этого места."
        )
        await port.emit(render(message, variables), keyboard=keyboard)
        return False
    skipped = node["allow_skip"] and text.strip().casefold() == "пропустить"
    value = normalize_contact(text, node["contact_type"])
    if not skipped and value is None:
        default_error = (
            "Введите телефон: от 10 до 15 цифр, можно с +, пробелами и скобками. Например +7 999 123-45-67."
            if node["contact_type"] == "phone"
            else "Введите email в формате name@example.com."
        )
        await port.emit(node["error_text"] or default_error, keyboard=keyboard)
        return False
    await port.cancel_contact_wait(
        state, "Сбор контакта пропущен" if skipped else "Контакт получен"
    )
    state.pop("waiting", None)
    variables.pop("_contact_reminder", None)
    if skipped:
        variables.pop(node["variable"], None)
    else:
        variables[node["variable"]] = value
        await port.save_contact(node["contact_type"], value)
    return True


async def advance(
    graph, state, text, payload, port, *, restart=False, resume_wait=False
):
    """Mutates a serializable state; all external effects go through port."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    if state.get("handoff") and not restart:
        return
    variables = state.setdefault("variables", {})
    if not resume_wait:
        variables["last_message"] = text
    current = nodes.get(state.get("node_id", ""))
    if restart or not current:
        state["handoff"] = False
        state.pop("waiting", None)
        variables.pop("_contact_reminder", None)
        state["stack"] = []
        current = next(n for n in graph["nodes"] if n["type"] == "start")
    elif current["type"] == "wait_reply":
        decision = (
            "timeout"
            if resume_wait
            else await port.reply_decision(current, state, text, payload)
        )
        if decision not in {"answer", "timeout"}:
            if decision == "cancelled":
                state["node_id"], state["stack"] = "", []
                state.pop("waiting", None)
                await port.emit(
                    "Ожидание отменено. Напишите «меню», чтобы начать заново."
                )
            elif decision == "expired":
                await port.emit(
                    "Время для ответа уже вышло. Бот продолжит сценарий автоматически."
                )
            else:
                await port.emit(
                    "Пожалуйста, ответьте текстом. Время ожидания не продлевается."
                )
            return
        state.pop("waiting", None)
        if decision == "answer":
            variables[current["variable"]] = text[:1000]
        current = nodes[current["yes"] if decision == "answer" else current["no"]]
    elif current["type"] == "wait":
        if not resume_wait:
            return  # Incoming text/buttons never skip or restart a timer.
        state.pop("waiting", None)
        current = nodes[current["next"]]
    elif current["type"] == "contact":
        if not await accept_contact(
            current, state, text, payload, port, resume_wait=resume_wait
        ):
            return
        current = nodes[current["next"]]
    elif payload:
        valid = (
            current["type"] == "message"
            and payload.get("node") == current["id"]
            and payload.get("flow") == state.get("version")
            and payload.get("nonce") == state.get("nonce")
        )
        index = payload.get("button")
        if (
            not valid
            or type(index) is not int
            or not 0 <= index < len(current["buttons"])
            or current["buttons"][index]["kind"] != "next"
        ):
            await port.emit(
                "Эта кнопка устарела. Напишите «меню», чтобы начать заново."
            )
            return
        current = nodes[current["buttons"][index]["target"]]
    elif current["type"] == "question":
        if not text.strip():
            await port.emit("Пожалуйста, ответьте текстом.")
            return
        variables[current["variable"]] = text[:1000]
        target = next(
            (r["target"] for r in current["rules"] if matches(text, r["words"])),
            current["next"],
        )
        current = nodes[target]
    elif current["type"] == "message" and current["buttons"]:
        button = next(
            (
                b
                for b in current["buttons"]
                if b["kind"] == "next"
                and b["label"].casefold() == text.strip().casefold()
            ),
            None,
        )
        if not button:
            await port.emit(
                "Выберите кнопку ниже или напишите «меню».",
                keyboard=keyboard_for(
                    current, state["version"], state.get("nonce", "")
                ),
            )
            return
        current = nodes[button["target"]]
    else:
        current = next(n for n in graph["nodes"] if n["type"] == "start")
    for step in range(100):
        state["node_id"] = current["id"]
        kind = current["type"]
        if kind == "call_subflow":
            stack = state.setdefault("stack", [])
            if len(stack) >= MAX_CALL_DEPTH:
                raise ValueError("Превышена вложенность подцепочек")
            stack.append(
                {
                    "return_to": current["next"],
                    "variables": dict(variables),
                    "exports": current["return_variables"],
                }
            )
            names = {
                *current["pass_variables"],
                "first_name",
                "user_name",
                "last_message",
            }
            state["variables"] = variables = {
                k: v for k, v in variables.items() if k in names
            }
            current = nodes[current["subflow_id"]]
            continue
        if kind == "return":
            stack = state.setdefault("stack", [])
            if not stack:
                raise ValueError("Возврат без вызова подцепочки")
            frame = stack.pop()
            exported = {k: variables[k] for k in frame["exports"] if k in variables}
            last_message = variables.get("last_message", "")
            state["variables"] = variables = {
                k: v for k, v in frame["variables"].items() if k not in frame["exports"]
            }
            variables.update(exported)
            variables["last_message"] = last_message
            current = nodes[frame["return_to"]]
            continue
        if kind == "tag":
            await port.change_tag(normalize_tag(current["tag"]), current["tag_action"])
        elif kind == "tag_condition":
            result = await port.has_tag(normalize_tag(current["tag"]))
            current = nodes[current["yes"] if result else current["no"]]
            continue
        elif kind == "phone_condition":
            result = await port.has_phone(current.get("phone_check_mode", "provided"))
            current = nodes[current["yes"] if result else current["no"]]
            continue
        elif kind == "schedule":
            result = in_schedule(current, port.now())
            current = nodes[current["yes"] if result else current["no"]]
            continue
        if kind in {"wait", "wait_reply"}:
            if kind == "wait_reply":
                variables.pop(current["variable"], None)
                await port.emit(
                    render(current["text"], variables),
                    media_id=current["media_id"],
                    keyboard={"one_time": False, "buttons": []},
                )
            state["waiting"] = {
                "node_id": current["id"],
                "seconds": wait_seconds(current),
                "kind": kind,
            }
            return
        if kind == "set_variable":
            variables[current["variable"]] = render(current["value"], variables)[:1000]
            current = nodes[current["next"]]
            continue
        if kind == "variable_condition":
            result = compare_variable(current, variables)
            current = nodes[current["yes"] if result else current["no"]]
            continue
        if kind == "condition":
            result = (
                matches(text, current["words"])
                if current["condition"] == "contains"
                else await port.check(current)
            )
            current = nodes[current["yes"] if result else current["no"]]
            continue
        if kind == "random":
            # Stable for retries of the same callback; different incoming events
            # can choose different variants. Repeats between events are allowed.
            seed = f"{port.nonce(step)}:{current['id']}"
            index = int.from_bytes(
                hashlib.sha256(seed.encode()).digest()[:8], "big"
            ) % len(current["variants"])
            await port.emit(
                render(current["variants"][index], variables),
                media_id=current["media_id"],
                keyboard={"one_time": False, "buttons": []},
            )
        elif kind in {"message", "question", "contact"}:
            # New nonce on each visit prevents a button from an older visit being reused.
            state["nonce"] = port.nonce(step)
            keyboard = (
                keyboard_for(current, state["version"], state["nonce"])
                if kind == "message"
                else contact_keyboard(current, state)
                if kind == "contact"
                else {"one_time": False, "buttons": []}
            )
            if kind == "contact":
                arm_contact_reminder(current, state, "silence")
            prompt = render(current["text"], variables)
            if kind == "contact" and current["allow_skip"]:
                hint = (
                    "\n\nМожно написать «Пропустить», если не хотите оставлять контакт."
                )
                prompt = prompt[: 4000 - len(hint)] + hint
            await port.emit(
                prompt,
                keyboard=keyboard,
                media_id=current["media_id"],
            )
            if kind in {"question", "contact"} or current["buttons"]:
                return
        elif kind == "promo":
            await port.promo(current["campaign_id"], variables)
        elif kind == "operator":
            if hasattr(port, "sync_variables"):
                await port.sync_variables(variables)
            await port.handoff(render(current["text"], variables))
            state["handoff"] = True
            state["stack"] = []
            return
        elif kind == "end":
            if current["text"].strip():
                await port.emit(
                    render(current["text"], variables),
                    keyboard={"one_time": False, "buttons": []},
                )
            state["node_id"] = ""
            state["stack"] = []
            return
        current = nodes[current["next"]]
    raise ValueError("Слишком много шагов сценария без ответа пользователя")


def starter_graph():
    return Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "Начало диалога",
                    "x": 80,
                    "y": 190,
                    "next": "welcome",
                },
                {
                    "id": "welcome",
                    "type": "message",
                    "title": "Знакомство",
                    "x": 390,
                    "y": 130,
                    "text": "Привет, {first_name}! Рады видеть вас в SARKISIAN. Чем можем помочь?",
                    "buttons": [
                        {"label": "Подобрать товар", "target": "question"},
                        {
                            "label": "Магазин",
                            "kind": "link",
                            "url": "https://sarkisianbrand.ru/",
                        },
                        {
                            "label": "Позвать менеджера",
                            "target": "manager",
                            "color": "secondary",
                        },
                    ],
                },
                {
                    "id": "question",
                    "type": "question",
                    "title": "Узнать пожелания",
                    "x": 740,
                    "y": 80,
                    "text": "Расскажите, что ищете?",
                    "variable": "request",
                    "next": "thanks",
                },
                {
                    "id": "thanks",
                    "type": "message",
                    "title": "Подтвердить запрос",
                    "x": 1080,
                    "y": 80,
                    "text": "Спасибо! Ваш запрос: {request}. Передаю менеджеру.",
                    "next": "manager",
                },
                {
                    "id": "manager",
                    "type": "operator",
                    "title": "Менеджер",
                    "x": 740,
                    "y": 400,
                    "text": "Менеджер подключится к диалогу. Чтобы вернуться к боту, напишите «меню».",
                },
            ]
        }
    ).model_dump()
