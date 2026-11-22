# Развёртывание на сервере (Docker)

Один сервис `bot` с long polling и разовый сервис `cli` для запросов из
терминала. Порты не публикуются: бот сам ходит в Telegram и OpenRouter.
Требуется Docker с Compose v2 и исходящий HTTPS.

## Что лежит на хосте

| Путь | Роль |
|---|---|
| `chroma_db/` | база знаний; **не в git**, копируется на сервер и вшивается в образ при сборке |
| `Dockerfile`, `.dockerignore` | образ: Python 3.14, зависимости из `uv.lock`, код, база знаний |
| `docker-compose.yml` | сервисы `bot` и `cli`, volume для базы доступа |
| `deploy.sh` | проверки, резервная копия, сборка, healthcheck и откат |
| `.env` | секреты и настройки; создаётся из `.env.example`, в git не попадает |

`pdfs/`, `tests/` и локальная `.venv` в образ не попадают. База доступа
Telegram хранится в именованном volume `bot-data` (`/app/data`), поэтому
переживает пересоздание контейнера.

## Первый запуск

В GitHub лежит только код: `chroma_db/`, `pdfs/` и файл с ответами в репозиторий
не попадают. После клонирования базу знаний нужно перенести с машины, где она
уже собрана, либо пересобрать её (см. [knowledge-base.md](knowledge-base.md)).

```bash
# 1. На сервере: клонировать код.
git clone https://github.com/egoricsss/RAG_agent.git rag-agent
cd rag-agent

# 2. С машины с готовой базой: перенести chroma_db (~14 МБ).
rsync -av chroma_db/ server:/opt/rag-agent/chroma_db/
# без rsync: scp -r chroma_db server:/opt/rag-agent/

# 3. Убедиться, что база на месте.
test -f chroma_db/chroma.sqlite3 && echo "база на месте"

# 4. Настроить окружение.
cp .env.example .env
chmod 600 .env
$EDITOR .env          # OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_SUPER_ADMIN_ID

# 5. Собрать и запустить.
docker compose up -d --build

# 6. Проверка.
docker compose ps
docker compose logs -f bot
```

Если папки `chroma_db` нет, сборка завершится ошибкой `COPY failed`; если папка
пустая, контейнер упадёт с сообщением `База ChromaDB не найдена`. После старта
отправьте боту `/start` с аккаунта супер-администратора.

`TELEGRAM_ALLOWED_USER_IDS` действует только при создании базы доступа; дальше
участниками управляют команды `/add_user`, `/remove_user`, `/remove_all`.

## Переменные окружения

Обязательные и необязательные переменные перечислены с комментариями в
[`.env.example`](../.env.example). Кратко:

| Переменная | Обязательна | По умолчанию |
|---|---|---|
| `OPENROUTER_API_KEY` | да | — |
| `TELEGRAM_BOT_TOKEN` | да | — |
| `TELEGRAM_SUPER_ADMIN_ID` | да | — |
| `TELEGRAM_ALLOWED_USER_IDS` | нет | пусто |
| `OPENROUTER_CHAT_MODEL` | нет | `google/gemini-2.5-flash` |
| `OPENROUTER_EMBEDDING_MODEL` | нет | `qwen/qwen3-embedding-8b` |
| `OPENROUTER_VISION_MODEL` | нет | `google/gemini-2.5-flash` |
| `CHROMA_COLLECTION` | нет | `compilers_kb` |

`CHROMA_DB_PATH` и `TELEGRAM_ACCESS_DB_PATH` в контейнере фиксированы
(`/app/chroma_db` и `/app/data/telegram_access.sqlite3`) и заданы в
`docker-compose.yml`. Код `.env` не читает: файл подхватывает Compose и передаёт
переменные в окружение контейнера, поэтому запуск бота без Compose потребует
экспортировать переменные вручную.

## CLI-запрос

```bash
docker compose run --rm cli "Что такое транслирующая грамматика?"
```

Сервис `cli` использует тот же образ, `.env` и volume с базой доступа. Профиль
`cli` не поднимается вместе с `bot`: `docker compose up` запускает только бота.

## Безопасный деплой скриптом

`deploy.sh` обновляет сервис одной командой и не останавливает работающий
контейнер, пока новый образ не собран и не проверен.

| Команда | Действие |
|---|---|
| `./deploy.sh` | проверки, резервная копия, сборка, проверка образа, запуск, healthcheck, откат при сбое |
| `./deploy.sh --check` | только собрать и проверить образ; контейнер не трогать |
| `./deploy.sh --rollback` | вернуть предыдущий образ `rag-agent:rollback` |
| `./deploy.sh --no-pull` | не обновлять базовый образ при сборке (`uv.lock` всё равно фиксирует зависимости) |
| `./deploy.sh --allow-dirty` | разрешить деплой незакоммиченных изменений |

До сборки скрипт проверяет Docker и Compose v2, `.env` (права `600`,
обязательные поля заполнены, файл не отслеживается git), `.dockerignore`
исключает `.env`, Compose не публикует порты и включает
`no-new-privileges`/`cap_drop: ALL`, база знаний на месте, хватает места на
диске. После сборки образ запускается от непривилегированного пользователя и
проверяется на отсутствие `.env`, базы доступа, `tests/`, `pdfs/` и `.git`.
Перед заменой контейнера база доступа копируется в `backups/` (хранятся
последние 10 копий, `chmod 600`); каталог в `.gitignore`.

Если новый контейнер не стал `healthy` за `HEALTH_TIMEOUT` секунд
(по умолчанию 180), скрипт печатает логи, возвращает тег `rag-agent:rollback`
и поднимает предыдущую версию. Незакоммиченные изменения по умолчанию
запрещены: на сервере должен работать известный коммит.

`./deploy.sh --check` можно запускать и локально: он собирает образ и выполняет
те же проверки безопасности, не запуская бота.

## Обновление вручную

Обычно достаточно `./deploy.sh`. Без скрипта:

```bash
git pull
docker compose build --pull
docker compose up -d
```

База знаний вшита в образ. Если `chroma_db` обновили на хосте, нужна
пересборка (`docker compose build`), иначе контейнер продолжит использовать
старую копию. Версии зависимостей фиксирует `uv.lock`.

## Резервная копия доступа

```bash
docker compose exec -T bot cat /app/data/telegram_access.sqlite3 > access-backup.sqlite3
```

Восстановление:

```bash
docker compose stop bot
docker compose run --rm --no-deps --entrypoint sh bot -c \
  'cat > /app/data/telegram_access.sqlite3' < access-backup.sqlite3
docker compose start bot
```

Файл базы доступа содержит ID и username участников: храните копию и `.env` так
же бережно, как токен бота.

## Диагностика

| Симптом | Причина |
|---|---|
| `Укажите OPENROUTER_API_KEY` | пустой или отсутствующий `.env` |
| `Укажите TELEGRAM_BOT_TOKEN` / `TELEGRAM_SUPER_ADMIN_ID` | не заполнены обязательные поля |
| `База ChromaDB не найдена` | образ собран без `chroma_db` |
| `Коллекция использует модель эмбеддингов …` | `OPENROUTER_EMBEDDING_MODEL` не совпадает с моделью базы |
| `database is locked` / `readonly database` | второй экземпляр бота или чужой `/app/chroma_db` |
| `Conflict: terminated by other getUpdates` | запущено два процесса с одним токеном |
| `unhealthy` в `docker compose ps` | процесс бота завершился; смотрите `docker compose logs bot` |

Один токен — один экземпляр. Не масштабируйте `bot` и не запускайте его
одновременно с локальным `uv run telegram_bot.py` на том же токене.

Ограничения ресурсов не заданы: агенту хватает памяти, а узкое место — сеть и
оплата моделей. Образ занимает около 2 ГБ (chromadb, onnxruntime, grpcio).
Логи ограничены тремя файлами по 10 МиБ.
