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

## 7. Мини-приложение «Статистика» (Cloudflare, бесплатно)

Мини-приложение — статичная страница `webapp/index.html`, данные она берёт из OpenDota сама.
Ей нужен адрес с HTTPS — его бесплатно даёт Cloudflare. Настройки выкладки лежат в `wrangler.jsonc`.
Запросы к OpenDota идут через посредника `worker.js` на том же адресе (`/api/…`): из части сетей
api.opendota.com недоступен напрямую, а Cloudflare доступен; ответы он ещё и кэширует.

1. Зарегистрируйся на https://dash.cloudflare.com (карта не нужна).
2. **Workers & Pages** → **Create** → **Import a repository** (подключи GitHub) → `dota-match-tracker`.
3. На экране «Set up your application» оставь всё как есть:
   - **Build command**: пусто
   - **Deploy command**: `npx wrangler deploy`
4. **Deploy**. Через минуту появится адрес вида `https://dota-match-tracker.<имя>.workers.dev` —
   открой его в браузере: должна показаться статистика на примерных данных.
5. Впиши адрес в `.env` бота: `WEBAPP_URL=https://dota-match-tracker.<имя>.workers.dev` и перезапусти бота.

После этого у всех пользователей появится кнопка **«Статистика»** слева от поля ввода, а в профиле —
кнопка «Открыть статистику». При каждом пуше в `main` Cloudflare сам обновляет страницу.
