"""Execution history and opt-in manager notifications for the live interpreter."""

import hashlib

import httpx

from . import clients, vk_api
from .config import get_settings
from .operators import configured_operators


class JourneyPort:
    def init_journey(self):
        self.journey = []
        self.scenario_info = {}
        self.pending_notices = []

    def bind_scenario(self, scenario):
        self.scenario_info = {
            "scenario_id": scenario.id,
            "scenario": scenario.title,
            "version": scenario.version,
        }

    async def trace(self, phase, node=None, detail="", variables=None):
        if len(self.journey) >= 210:
            return
        node = node or {}
        self.journey.append(
            {
                **self.scenario_info,
                "date": clients.now().isoformat() + "Z",
                "phase": phase,
                "node_id": node.get("id", ""),
                "node": node.get("title", "") or node.get("id", ""),
                "type": node.get("type", ""),
                "detail": detail[:1000],
            }
        )
        if phase == "entered" and node.get("notify_manager"):
            self.pending_notices.append(
                (
                    f"stage:{len(self.journey)}",
                    f"Клиент дошёл до этапа: {node.get('title', node.get('id', ''))}",
                    dict(variables or {}),
                )
            )

    async def contacts_received(self, node, values, variables):
        if self.values.get("notify_contacts") == "true":
            self.pending_notices.append(
                (f"contact:{node['id']}", "Клиент оставил контакты", dict(variables))
            )

    async def scenario_completed(self, variables):
        if self.values.get("notify_completed") == "true":
            self.pending_notices.append(
                ("completed", "Клиент завершил сценарий", dict(variables))
            )

    async def finish_notices(self):
        if not self.pending_notices:
            return
        operators = configured_operators(self.values, get_settings())
        if not operators:
            self.warning = (
                self.warning + "\nУведомления включены, но ID менеджеров не заданы."
            ).strip()
            return
        for key, title, variables in self.pending_notices:
            details = "\n".join(
                f"{k}: {v}" for k, v in variables.items() if not k.startswith("_")
            )[:2600]
            message = f"{title}\nСценарий: {self.scenario_info.get('scenario', '—')}\nКлиент: https://vk.com/id{self.user_id}\nДиалог: https://vk.com/gim{get_settings().vk_group_id}?sel={self.user_id}\n{details}"
            for operator in operators:
                random_id = (
                    int.from_bytes(
                        hashlib.sha256(
                            f"notice:{self.event_key}:{key}:{operator}".encode()
                        ).digest()[:4],
                        "big",
                    )
                    & 0x7FFFFFFF
                )
                try:
                    await vk_api.send_message(
                        operator, message[:4000], random_id=random_id or 1
                    )
                except (vk_api.VkApiError, httpx.HTTPError) as error:
                    code = error.code if isinstance(error, vk_api.VkApiError) else None
                    reason = (
                        f"код VK {code}" if code is not None else "ошибка связи с VK"
                    )
                    self.warning = (
                        self.warning
                        + f"\nУведомление менеджеру {operator} не доставлено: {reason}."
                    )[-1000:].strip()
        self.pending_notices.clear()
