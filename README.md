# VK Comment Promo Bot

Production-заготовка: VK-бот, веб-админка, PostgreSQL и Docker Compose.

## Что есть сейчас

- обработка событий Callback API VK;
- проверка подписки на сообщество;
- отправка промокода в личные сообщения;
- защита от повторной обработки комментариев;
- веб-панель `/admin` с отдельной страницей входа, сессией и выходом;
- редактирование промокода, текста, ссылки и списка постов без перезапуска;
- статистика и журнал последних событий;
- контейнеризация приложения и PostgreSQL.

## Запуск на VPS

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f vk-bot
```

Панель после запуска: `http://127.0.0.1:8000/admin`. Логин и пароль берутся из `ADMIN_USERNAME` и `ADMIN_PASSWORD`. Для публичного доступа подключим домен через Nginx и HTTPS. Portainer устанавливается отдельно на VPS и управляет этим Compose-проектом.

В `.env` обязательно заменить все значения `change_this_*`, указать ключ сообщества VK и параметры PostgreSQL. `DATABASE_URL` должен быть:

```text
postgresql+psycopg://vkbot:ПАРОЛЬ@postgres:5432/vkbot
```

В VK включается Callback API с URL `https://ВАШ-ДОМЕН/vk/callback`, секретом, кодом подтверждения и событием `wall_reply_new`.
