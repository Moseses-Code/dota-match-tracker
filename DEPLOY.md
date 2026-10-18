# Запуск на Bot-Hosting.net

Бесплатно, без карты, 256 МБ памяти (боту нужно ~150–200 МБ).
Панель — Pterodactyl; названия полей ниже могут немного отличаться.

## 1. Токен GitHub для приватного репозитория

Репозиторий приватный, поэтому серверу нужен токен только на чтение.

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token.
2. Repository access: **Only select repositories** → `dota-match-tracker`.
3. Permissions → Repository → **Contents: Read-only**.
4. Срок — хоть «No expiration». Скопируй токен (`github_pat_…`).

## 2. Сервер

1. Зарегистрируйся на https://bot-hosting.net и создай сервер с языком **Python**.
2. Startup / настройки сервера:
   - **Docker image / версия Python**: 3.11 или новее (лучше 3.12)
   - **Git Repo Address**: `https://github.com/Moseses-Code/dota-match-tracker.git`
   - **Install Branch**: `main`
   - **Git Username**: `Moseses-Code`
   - **Git Access Token**: токен из шага 1
   - **Auto Update**: включить (бот будет подтягивать новый код при каждом запуске)
   - **App py file**: `main.py`
   - **Requirements file**: `requirements.txt`
3. Нажми **Reinstall** (или Install), чтобы сервер скачал код из GitHub.

## 3. Токены бота

В **File Manager** создай в корне файл `.env` и вставь содержимое своего локального `.env`:

```
BOT_TOKEN=...
STRATZ_TOKEN=...
OWNER_ID=...
POLL_ACTIVE=180
POLL_IDLE=600
PARSE_WAIT_MIN=25
MMR_STEP=25
```

`.env` не хранится в GitHub — только на сервере.

## 4. База пользователей (необязательно)

Чтобы не регистрироваться заново, загрузи через File Manager файл `bot.db` в папку `data/`
(создай папку, если её нет). Иначе просто нажми в боте /start ещё раз.

## 5. Запуск

1. **Останови бота везде, где он ещё запущен** (на ПК). Два бота с одним токеном мешают друг другу,
   а STRATZ пускает не больше 2 IP-адресов за 15 минут.
2. Нажми **Start**. В консоли должно появиться `Run polling for bot @DotaMatchTracker_bot`.

## 6. Не забывать

- Раз в **4 дня** зайди в панель и нажми продление сервера, иначе его остановят.
- Обновить бота: запушить код в GitHub и перезапустить сервер (Auto Update сделает `git pull`).

## 7. Мини-приложение «Статистика»

Мини-приложение (`webapp/index.html`) отдаёт сам бот — встроенный веб-сервер (`webserver.py`) слушает порт,
который Bot-Hosting задаёт в `SERVER_PORT`. Cloudflare (workers.dev) из России душат, поэтому страница живёт
на адресе сервера бота.

1. В панели Bot-Hosting откройте вкладку **Domains** → **Activate domains**. Появится адрес вида
   `https://<имя>.apps.bot-hosting.cloud`.
2. На вкладке **Environment** поставьте `WEBAPP_URL` = этот адрес.
3. Перезапустите сервер. В консоли должна появиться строка `webapp server on port …`.
4. Откройте адрес в браузере — должна показаться статистика на примерных данных.

Данные страница берёт из OpenDota сама, со своего устройства; если напрямую не выходит — через `/api/`
на сервере бота. `worker.js` и `wrangler.jsonc` — запасной вариант выкладки на Cloudflare Workers
(без России работает так же).
