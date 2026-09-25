"""Validated graph and shared interpreter for VK and the admin simulator."""

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

NodeId = str
KINDS = {"start", "message", "question", "condition", "promo", "operator", "end"}
VARIABLE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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
        "start", "message", "question", "condition", "promo", "operator", "end"
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


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list, max_length=100)


def outputs(node):
    kind = node["type"]
    if kind == "condition":
        return [("Да", node["yes"]), ("Нет", node["no"])]
    if kind in {"end", "operator"}:
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
        if (
            n["type"] in {"message", "question"}
            and not n["text"].strip()
            and not n["media_id"]
        ):
            errors.append(f"{prefix}: добавьте текст или файл.")
        if n["type"] == "question" and (
            not VARIABLE.fullmatch(n["variable"])
            or n["variable"] in {"first_name", "user_name", "promo_code", "shop_url"}
        ):
            errors.append(f"{prefix}: имя ответа — латиница, цифры и _, например size.")
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
        if n["type"] == "question" or (n["type"] == "message" and n["buttons"]):
            return False
        visiting.add(node_id)
        cycle = any(auto_cycle(t) for _, t in outputs(n))
        visiting.remove(node_id)
        done.add(node_id)
        return cycle

    if any(auto_cycle(n["id"]) for n in nodes):
        errors.append("Обнаружен бесконечный цикл без ожидания ответа клиента.")
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


async def advance(graph, state, text, payload, port, *, restart=False):
    """Mutates a serializable state; all external effects go through port."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    if state.get("handoff") and not restart:
        return
    variables = state.setdefault("variables", {})
    variables["last_message"] = text
    current = nodes.get(state.get("node_id", ""))
    if restart or not current:
        state["handoff"] = False
        current = next(n for n in graph["nodes"] if n["type"] == "start")
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
        if kind == "condition":
            result = (
                matches(text, current["words"])
                if current["condition"] == "contains"
                else await port.check(current)
            )
            current = nodes[current["yes"] if result else current["no"]]
            continue
        if kind in {"message", "question"}:
            # New nonce on each visit prevents a button from an older visit being reused.
            state["nonce"] = port.nonce(step)
            keyboard = (
                keyboard_for(current, state["version"], state["nonce"])
                if kind == "message"
                else {"one_time": False, "buttons": []}
            )
            await port.emit(
                render(current["text"], variables),
                keyboard=keyboard,
                media_id=current["media_id"],
            )
            if kind == "question" or current["buttons"]:
                return
        elif kind == "promo":
            await port.promo(current["campaign_id"], variables)
        elif kind == "operator":
            await port.handoff(render(current["text"], variables))
            state["handoff"] = True
            return
        elif kind == "end":
            if current["text"].strip():
                await port.emit(
                    render(current["text"], variables),
                    keyboard={"one_time": False, "buttons": []},
                )
            state["node_id"] = ""
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
