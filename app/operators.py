"""Manager settings and shared handoff state helpers."""

import re


def parse_operator_ids(value):
    parts = [part for part in re.split(r"[,;\s]+", str(value or "").strip()) if part]
    if any(not re.fullmatch(r"[0-9]+", part) for part in parts):
        raise ValueError(
            "Укажите числовые ID менеджеров через запятую или с новой строки."
        )
    ids = list(dict.fromkeys(int(part) for part in parts))
    if any(not 0 < ident < 2**53 for ident in ids) or len(ids) > 50:
        raise ValueError("Укажите до 50 положительных числовых ID менеджеров.")
    return ids


def configured_operators(values, settings):
    # Keep the existing setting / ENV name, including single-manager installations.
    return parse_operator_ids(values.get("operator_user_id", settings.operator_user_id))


def reset_handoff(conversation):
    conversation.handoff = False
    conversation.assigned_operator_id = None
    conversation.assigned_at = None
    conversation.handoff_token = ""
    conversation.handoff_started_at = 0
    conversation.handoff_message_id = 0
