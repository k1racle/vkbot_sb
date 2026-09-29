"""Published entry rules. No database or VK side effects."""

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field


class EntryRule(BaseModel):
    mode: Literal["default", "keywords"] = "default"
    keywords: str = Field(default="", max_length=2000)
    match: Literal["contains", "exact"] = "contains"


def normalize_phrase(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", value))


def phrases(value: str) -> list[str]:
    return list(
        dict.fromkeys(
            normalize_phrase(part)
            for part in re.split(r"[,;\n]+", value)
            if part.strip()
        )
    )


def entry_rule(graph: dict | None) -> dict:
    # Old published graphs remain default scenarios without rewriting storage.
    return (graph or {}).get("entry", {})


def keyword_graph(graph: dict | None) -> bool:
    return entry_rule(graph).get("mode", "default") == "keywords"


def entry_errors(graph: dict) -> list[str]:
    if not keyword_graph(graph):
        return []
    words = phrases(entry_rule(graph).get("keywords", ""))
    if not words or "" in words:
        return [
            "Запуск сценария: добавьте ключевые слова или фразы, не только знаки препинания."
        ]
    if len(words) > 30:
        return ["Запуск сценария: допускается не больше 30 разных фраз."]
    reserved = {
        "меню",
        "начать",
        "старт",
        "start",
        "стоп",
        "stop",
        "отписаться",
        "отписаться от рассылок",
        "подписаться на рассылку",
        "subscribe",
    }
    if reserved.intersection(words):
        return [
            "Запуск сценария: команды «Меню», «Начать», «Стоп» и управления рассылками зарезервированы."
        ]
    return []


def choose_keyword(scenarios, text: str):
    """One winner: exact match, then longest phrase, then oldest scenario ID."""
    message = normalize_phrase(text)
    best, result = None, None
    for scenario in scenarios:
        if not keyword_graph(scenario.published):
            continue
        rule = entry_rule(scenario.published)
        exact = rule.get("match", "contains") == "exact"
        for phrase in phrases(rule.get("keywords", "")):
            if phrase and (
                message == phrase if exact else f" {phrase} " in f" {message} "
            ):
                score = (int(exact), len(phrase), -scenario.id)
                if best is None or score > best:
                    best, result = score, scenario
    return result


def choose_scenario(scenarios, conversation, text, *, restart=False, has_payload=False):
    """Continue an ongoing keyword dialogue; ordinary replies aren't new triggers."""
    triggered = None if restart or has_payload else choose_keyword(scenarios, text)
    if triggered is not None:
        return triggered, True
    default = next((s for s in scenarios if not keyword_graph(s.published)), None)
    if not restart and conversation.node_id:
        current = next((s for s in scenarios if s.id == conversation.scenario_id), None)
        if current is not None:
            return current, False
    return default, False
