"""Pure rules for schedules, client tags and reusable local subflows."""

import re
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_CALL_DEPTH = 5
MAX_TAGS = 100


def normalize_tag(value):
    value = " ".join(value.split()).casefold()
    if not re.fullmatch(r"[\w -]{1,40}", value):
        raise ValueError("Метка: от 1 до 40 букв, цифр, пробелов, - и _.")
    return value


def clock_minutes(value, *, end=False):
    if end and value == "24:00":
        return 1440
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(
            "Укажите время ЧЧ:ММ; 24:00 допустимо только для конца интервала."
        )
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def schedule_errors(node):
    errors = []
    try:
        ZoneInfo(node["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("Неизвестный часовой пояс, например Europe/Moscow.")
    try:
        start = clock_minutes(node["time_from"])
        end = clock_minutes(node["time_to"], end=True)
        if start == end:
            errors.append("Начало и конец не должны совпадать. Весь день: 00:00–24:00.")
    except ValueError as error:
        errors.append(str(error))
    if not node["weekdays"] or len(set(node["weekdays"])) != len(node["weekdays"]):
        errors.append("Выберите хотя бы один день недели без повторов.")
    try:
        dates = [
            date.fromisoformat(node[k]) if node[k] else None
            for k in ("date_from", "date_to")
        ]
        if all(dates) and dates[0] > dates[1]:
            errors.append("Дата начала должна быть не позже даты окончания.")
    except ValueError:
        errors.append("Укажите действительную дату в формате ГГГГ-ММ-ДД.")
    return errors


def in_schedule(node, now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo(node["timezone"]))
    minute = local.hour * 60 + local.minute
    start, end = (
        clock_minutes(node["time_from"]),
        clock_minutes(node["time_to"], end=True),
    )
    shift_date = local.date()
    if start < end:
        inside = start <= minute < end
    else:
        inside = minute >= start or minute < end
        if minute < end:
            shift_date -= timedelta(days=1)
    return bool(
        inside
        and shift_date.weekday() in node["weekdays"]
        and (
            not node["date_from"] or shift_date >= date.fromisoformat(node["date_from"])
        )
        and (not node["date_to"] or shift_date <= date.fromisoformat(node["date_to"]))
    )


def subflow_errors(nodes, outputs):
    by_id = {n["id"]: n for n in nodes}
    roots = [n for n in nodes if n["type"] in {"start", "subflow"}]
    owners, calls, errors = {}, {}, []
    for root in roots:
        seen, pending = set(), [root["id"]]
        calls[root["id"]] = set()
        while pending:
            ident = pending.pop()
            if ident in seen or ident not in by_id:
                continue
            seen.add(ident)
            node = by_id[ident]
            previous = owners.setdefault(ident, root["id"])
            if previous != root["id"]:
                errors.append(
                    f"{node['title']}: цепочки пересекаются. Используйте «Вызвать подцепочку» и «Возврат»."
                )
            if root["type"] == "start" and node["type"] == "return":
                errors.append(
                    f"{node['title']}: «Возврат» допустим только внутри подцепочки."
                )
            implicit_end = any(not target for _, target in outputs(node)) or (
                node["type"] == "message"
                and node["buttons"]
                and all(b["kind"] == "link" for b in node["buttons"])
            )
            if root["type"] == "subflow" and (node["type"] == "end" or implicit_end):
                errors.append(
                    f"{node['title']}: завершите подцепочку блоком «Возврат», а не «Завершение» или пустым переходом."
                )
            if node["type"] == "call_subflow":
                target = by_id.get(node["subflow_id"])
                if not target or target["type"] != "subflow":
                    errors.append(
                        f"{node['title']}: выберите вход существующей подцепочки."
                    )
                else:
                    calls[root["id"]].add(target["id"])
                pending.append(node["next"])
            else:
                pending.extend(t for _, t in outputs(node))

    depths = {}

    def depth(ident, path):
        if ident in path:
            errors.append("Подцепочки не могут вызывать себя или друг друга по кругу.")
            return MAX_CALL_DEPTH + 1
        if ident in depths:
            return depths[ident]
        result = max(
            (1 + depth(target, [*path, ident]) for target in calls.get(ident, ())),
            default=0,
        )
        depths[ident] = result
        return result

    for root in roots:
        if depth(root["id"], []) > MAX_CALL_DEPTH:
            errors.append(
                f"Вложенность подцепочек не должна превышать {MAX_CALL_DEPTH}."
            )
    return errors
