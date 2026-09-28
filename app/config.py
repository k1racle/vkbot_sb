from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    vk_api_version: str = "5.199"
    # Legacy bootstrap only. Once imported, projects use their own saved keys.
    vk_group_id: int = 0
    vk_group_token: str = ""
    vk_callback_secret: str = ""
    vk_confirmation_code: str = ""
    projects_encryption_key: str = ""
    projects_key_file: str = "data/projects.key"
    public_base_url: str = ""
    database_url: str = "sqlite:///./data/bot.db"
    promo_code: str = "WELCOME"
    promo_message: str = (
        "Спасибо за комментарий! 🎁\n\n"
        "Ваш промокод на скидку: {promo_code}\n\n"
        "Магазин: {shop_url}"
    )
    shop_url: str = "https://example.com"
    promo_attachments: str = ""
    allowed_post_ids: str = ""
    stop_words: str = ""
    min_comment_length: str = "1"
    one_promo_per_user: str = "true"
    test_mode: str = "false"
    test_trigger_phrase: str = "тестовое сообщение"
    admin_test_user_id: str = ""
    chat_enabled: str = "true"
    chat_greeting: str = "Привет! Я бот магазина. Напишите, чем помочь."
    operator_user_id: str = ""
    operator_trigger_words: str = "оператор,менеджер,человек"
    operator_ack: str = "Передал запрос менеджеру. Скоро с вами свяжутся."
    log_level: str = "INFO"
    background_jobs_enabled: bool = True
    admin_username: str = "admin"
    admin_password: str
    admin_session_secret: str

    @field_validator("public_base_url")
    @classmethod
    def validate_public_url(cls, value):
        value = value.strip().rstrip("/")
        if not value:
            return ""
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path
            or any(char.isspace() for char in value)
        ):
            raise ValueError(
                "PUBLIC_BASE_URL должен быть адресом сайта без пути, например https://vkbot.example.ru"
            )
        return value

    @property
    def post_ids(self) -> set[int] | None:
        if not self.allowed_post_ids.strip():
            return None
        return {
            int(value.strip())
            for value in self.allowed_post_ids.split(",")
            if value.strip()
        }


@lru_cache
def get_base_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    # ContextVars are copied into async tasks and FastAPI's threadpool. Never
    # mutate a global Settings instance when another group's callback arrives.
    from .projects import current_project, project_settings

    project = current_project.get()
    base = get_base_settings()
    return project_settings(project, base) if project is not None else base


class SettingsProxy:
    """Read-only compatibility facade; resolves every read in its task context."""

    def __getattr__(self, name):
        return getattr(get_settings(), name)
