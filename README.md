# VK Comment Promo Bot

Production-заготовка: VK-бот, веб-админка, PostgreSQL и Docker Compose.

## Что есть сейчас

- обработка событий Callback API VK;
- проверка подписки на сообщество;
- отправка промокода в личные сообщения;
- защита от повторной обработки комментариев;
- веб-панель `/admin` с отдельной страницей входа, сессией и выходом;
- редактирование промокода, текста, ссылки и списка постов без перезапуска;
- защита от повторной выдачи промокода одному пользователю;
- стоп-слова и минимальная длина комментария;
- отправка VK-вложений вместе с сообщением;
- статистика и журнал последних событий;
- контейнеризация приложения и PostgreSQL.

## Запуск на VPS

```bash
docker network create proxy
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f vk-bot
```

Панель после запуска: `http://127.0.0.1:8000/admin`. Логин и пароль берутся из `ADMIN_USERNAME` и `ADMIN_PASSWORD`. Portainer устанавливается отдельно на VPS и управляет этим Compose-проектом.

## Nginx Proxy Manager

В Portainer создай Stack из файла `deploy/nginx-proxy-manager.yml`. Перед этим один раз создай общую сеть:

```bash
docker network create proxy
```

После запуска Nginx Proxy Manager открой `http://IP_VPS:81`, создай Proxy Host для `bot.it-sarkisian.ru`, укажи схему `http`, имя контейнера `vk-bot` и порт `8000`. Затем запроси SSL-сертификат Let's Encrypt и включи Force SSL.

Вложения в панели задаются VK attachment ID через запятую, например `photo-123_456,video-123_789`. Стоп-слова можно вводить по одному на строку. При включённой защите один пользователь получает промокод только один раз.

В `.env` обязательно заменить все значения `change_this_*`, указать ключ сообщества VK и параметры PostgreSQL. `DATABASE_URL` должен быть:

```text
postgresql+psycopg://vkbot:ПАРОЛЬ@postgres:5432/vkbot
```

В VK включается Callback API с URL `https://ВАШ-ДОМЕН/vk/callback`, секретом, кодом подтверждения и событием `wall_reply_new`.
