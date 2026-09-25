from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    vk_api_version: str = "5.199"
    vk_group_id: int
    vk_group_token: str
    vk_callback_secret: str
    vk_confirmation_code: str
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
    log_level: str = "INFO"
    admin_username: str = "admin"
    admin_password: str
    admin_session_secret: str

    @property
    def post_ids(self) -> set[int] | None:
        if not self.allowed_post_ids.strip():
            return None
        return {int(value.strip()) for value in self.allowed_post_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
