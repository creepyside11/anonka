# anonka

Telegram-бот для анонимных сообщений + веб-панель управления с GitHub OAuth и автообновлением проектов.

## Возможности бота

- персональная ссылка для каждого пользователя;
- текст, фото, GIF, видео, кружки, голосовые, аудио, документы и стикеры;
- SQLite;
- кнопки меню и быстрый шаринг ссылки;
- статистика через Emerald Stats при наличии `EMERALD_STATS_URL` и `EMERALD_STATS_KEY`.

## Веб-панель

Панель запускается вместе с ботом и доступна на `PORT` (по умолчанию `8000`).

- дизайн в стиле Liquid Glass;
- GitHub OAuth в разделе профиля;
- список доступных публичных и приватных репозиториев;
- выбор доступной ветки;
- включение/выключение автообновления проекта;
- проверка выбранной ветки каждые 30 секунд;
- при новом SHA свежая версия репозитория автоматически синхронизируется в `projects/<github_id>/<repo>`.

GitHub access token хранится в `dashboard.db` в зашифрованном виде. Ключ шифрования выводится из `SESSION_SECRET`.

## Переменные окружения

Обязательные для Telegram-бота:

```bash
BOT_TOKEN=123456:ABCDEF
```

Для GitHub OAuth:

```bash
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
SESSION_SECRET=change-me-to-a-long-random-value
BASE_URL=https://your-domain.example
```

В GitHub OAuth App укажите callback URL:

```text
https://your-domain.example/auth/github/callback
```

OAuth запрашивает `repo read:user`, поэтому после подключения доступны и приватные репозитории, к которым у пользователя есть доступ.

Опционально:

```bash
PORT=8000
HOST=0.0.0.0
DASHBOARD_DB=dashboard.db
GITHUB_SYNC_ROOT=projects
DB_PATH=bot.db
EMERALD_STATS_URL=https://...
EMERALD_STATS_KEY=...
```

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

На Windows:

```bash
.venv\Scripts\activate
```

`main.py` одновременно запускает Telegram polling и веб-панель.
