#!/usr/bin/env bash
# Безопасный деплой RAG-агента в Docker.
#
#   ./deploy.sh                 собрать, проверить образ, заменить контейнер, откатить при сбое
#   ./deploy.sh --check         только собрать и проверить образ, не трогая запущенный контейнер
#   ./deploy.sh --rollback      вернуть предыдущий образ (rag-agent:rollback)
#   ./deploy.sh --no-pull       не обновлять базовый образ при сборке
#   ./deploy.sh --allow-dirty   разрешить деплой с незакоммиченными изменениями
#
# Порядок: проверки -> резервная копия -> сборка -> проверка безопасности образа ->
# запуск -> healthcheck -> автоматический откат. Текущий контейнер не останавливается,
# пока новый образ не собран и не проверен.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SERVICE=bot
CONTAINER=rag-agent-bot
IMAGE=rag-agent:local
ROLLBACK_IMAGE=rag-agent:rollback
BACKUP_DIR=${BACKUP_DIR:-backups}
KEEP_BACKUPS=${KEEP_BACKUPS:-10}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}
LOCK_FILE=.deploy.lock
MODE=deploy
PULL=1
ALLOW_DIRTY=0

log() { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy] ВНИМАНИЕ: %s\n' "$*" >&2; }
die() { printf '[deploy] ОШИБКА: %s\n' "$*" >&2; exit 1; }
trap 'die "команда завершилась с ошибкой (строка $LINENO)"' ERR

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --rollback) MODE=rollback ;;
        --no-pull) PULL=0 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $arg (см. --help)" ;;
    esac
done

# --- проверки до любых изменений -------------------------------------------

env_value() {
    # Значение KEY из .env без кавычек; секрет не печатается.
    local key=$1
    sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" .env \
        | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//' -e 's/[[:space:]]*$//'
}

preflight_common() {
    command -v docker >/dev/null || die "docker не найден"
    command -v flock >/dev/null || die "flock не найден (пакет util-linux)"
    docker info >/dev/null 2>&1 || die "Docker daemon недоступен"
    docker compose version >/dev/null 2>&1 \
        || die "нужен Docker Compose v2 (docker compose)"
    [[ -f docker-compose.yml ]] || die "нет docker-compose.yml"
    [[ -f Dockerfile ]] || die "нет Dockerfile"

    if [[ -d .git ]]; then
        git ls-files --error-unmatch .env >/dev/null 2>&1 \
            && die ".env отслеживается git — удалите его из индекса (git rm --cached .env)"
        local commit
        commit=$(git rev-parse --short HEAD 2>/dev/null || echo "без git")
        log "коммит: $commit"
    fi
}

preflight_deploy() {
    [[ -f .env ]] || die "нет .env (cp .env.example .env && chmod 600 .env)"
    chmod 600 .env
    log "права на .env: 600"

    local key value
    local missing=()
    for key in OPENROUTER_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_SUPER_ADMIN_ID; do
        value=$(env_value "$key")
        [[ -n "$value" ]] || missing+=("$key")
    done
    if (( ${#missing[@]} )); then
        local joined
        joined=$(IFS=', '; echo "${missing[*]}")
        if [[ "$MODE" == check ]]; then
            warn "в .env не заполнено: $joined — сборка не требует секретов, запуск требует"
        else
            die "в .env не заполнено: $joined"
        fi
    else
        log "обязательные переменные .env заполнены"
    fi

    [[ -f .dockerignore ]] || die "нет .dockerignore"
    grep -qxF '.env' .dockerignore \
        || die ".dockerignore должен исключать .env из контекста сборки"
    grep -qE '^\.env\.\*' .dockerignore \
        || warn "в .dockerignore нет шаблона .env.*"

    grep -qE '^[[:space:]]*ports:' docker-compose.yml \
        && die "docker-compose.yml публикует порты; боту они не нужны"
    grep -q 'no-new-privileges:true' docker-compose.yml \
        || die "в docker-compose.yml нет security_opt no-new-privileges:true"
    grep -q 'cap_drop' docker-compose.yml \
        || die "в docker-compose.yml нет cap_drop: ALL"

    docker compose config --quiet || die "docker compose config не проходит"
    log "docker-compose.yml корректен"

    if [[ -d .git && -n "$(git status --porcelain)" ]]; then
        if (( ALLOW_DIRTY )); then
            warn "есть незакоммиченные изменения — собирается рабочая копия, а не коммит"
        elif [[ "$MODE" == deploy ]]; then
            die "есть незакоммиченные изменения; закоммитьте их или добавьте --allow-dirty"
        else
            warn "есть незакоммиченные изменения — проверяется рабочая копия"
        fi
    fi

    [[ -s chroma_db/chroma.sqlite3 ]] \
        || die "нет chroma_db/chroma.sqlite3: перенесите базу знаний на сервер"

    local avail_mb
    avail_mb=$(df -Pm . | awk 'NR==2 {print $4}')
    (( avail_mb >= 4096 )) \
        || warn "свободно ${avail_mb} МиБ; для сборки образа ~2 ГБ лучше иметь от 4 ГиБ"
}

# --- резервная копия базы доступа ------------------------------------------

backup_access_db() {
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    local running
    running=$(docker compose ps --status running --services 2>/dev/null || true)
    if ! grep -qx "$SERVICE" <<<"$running"; then
        log "контейнер не запущен — резервная копия базы доступа не нужна"
        return 0
    fi
    if ! docker compose exec -T "$SERVICE" test -f /app/data/telegram_access.sqlite3; then
        log "база доступа ещё не создана — копировать нечего"
        return 0
    fi
    local backup="$BACKUP_DIR/access-$(date +%Y%m%d-%H%M%S).sqlite3"
    # sqlite backup API даёт согласованную копию работающей базы.
    docker compose exec -T "$SERVICE" python - <<'PY'
import sqlite3

src = sqlite3.connect("/app/data/telegram_access.sqlite3")
dst = sqlite3.connect("/tmp/telegram_access.backup.sqlite3")
with dst:
    src.backup(dst)
src.close()
dst.close()
PY
    docker compose exec -T "$SERVICE" cat /tmp/telegram_access.backup.sqlite3 > "$backup"
    docker compose exec -T "$SERVICE" rm -f /tmp/telegram_access.backup.sqlite3
    chmod 600 "$backup"
    [[ "$(head -c 16 "$backup")" == "SQLite format 3" ]] \
        || die "резервная копия $backup повреждена"
    log "резервная копия: $backup"

    # Оставляем только последние KEEP_BACKUPS копий.
    find "$BACKUP_DIR" -maxdepth 1 -name 'access-*.sqlite3' -printf '%T@ %p\n' \
        | sort -rn | tail -n +"$((KEEP_BACKUPS + 1))" | cut -d' ' -f2- \
        | while read -r old; do rm -f -- "$old"; done
}

# --- сборка и проверка безопасности образа ---------------------------------

build_image() {
    if [[ "$MODE" == check ]]; then
        log "режим проверки: тег $ROLLBACK_IMAGE не меняется"
    elif docker image inspect "$IMAGE" >/dev/null 2>&1; then
        docker tag "$IMAGE" "$ROLLBACK_IMAGE"
        log "текущий образ сохранён как $ROLLBACK_IMAGE"
    else
        log "текущего образа нет — откатываться будет некуда"
    fi

    if (( PULL )); then
        log "сборка с обновлением базового образа (зависимости фиксирует uv.lock)"
        docker compose build --pull "$SERVICE"
    else
        log "сборка без --pull"
        docker compose build "$SERVICE"
    fi
    docker image inspect "$IMAGE" >/dev/null 2>&1 \
        || die "после сборки нет образа $IMAGE"
}

verify_image() {
    log "проверка образа перед запуском"

    local uid
    uid=$(docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --entrypoint id "$IMAGE" -u)
    [[ "$uid" != "0" ]] || die "образ запускается от root"
    log "непривилегированный пользователь: uid=$uid"

    docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --entrypoint sh "$IMAGE" -c '
            test -s /app/chroma_db/chroma.sqlite3 || { echo "нет базы знаний"; exit 1; }
            test -x /app/.venv/bin/python     || { echo "нет venv"; exit 1; }
            test ! -e /app/.env               || { echo "в образ попал .env"; exit 1; }
            test ! -e /app/data/telegram_access.sqlite3 \
                || { echo "в образ попала база доступа"; exit 1; }
            test ! -e /app/tests              || { echo "в образ попали tests"; exit 1; }
            test ! -e /app/pdfs               || { echo "в образ попали pdfs"; exit 1; }
            test ! -e /app/.git               || { echo "в образ попал .git"; exit 1; }
        ' || die "образ содержит лишние или секретные файлы"
    log "в образе нет .env, базы доступа, tests, pdfs и .git; база знаний на месте"

    if docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE" \
        | grep -Eq '^(OPENROUTER_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_SUPER_ADMIN_ID)='; then
        die "секреты попали в ENV образа (Environment в Dockerfile)"
    fi
    log "секретов в переменных образа нет"

    local size
    size=$(docker image inspect --format '{{.Size}}' "$IMAGE")
    log "образ $IMAGE: $((size / 1024 / 1024)) МиБ"
}

# --- запуск и healthcheck --------------------------------------------------

wait_healthy() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) status
    while (( SECONDS < deadline )); do
        status=$(docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$CONTAINER" 2>/dev/null || true)
        case "$status" in
            healthy) log "healthcheck: healthy"; return 0 ;;
            unhealthy|exited|dead)
                log "healthcheck: $status"
                return 1
                ;;
        esac
        sleep 3
    done
    log "healthcheck не прошёл за ${HEALTH_TIMEOUT}с (последний статус: ${status:-нет контейнера})"
    return 1
}

start_bot() {
    log "запуск контейнера"
    docker compose up -d --no-build --remove-orphans "$SERVICE"
}

rollback() {
    if ! docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
        die "нет образа $ROLLBACK_IMAGE для отката; контейнер оставлен для диагностики"
    fi
    warn "откат на предыдущий образ"
    docker tag "$ROLLBACK_IMAGE" "$IMAGE"
    start_bot
    wait_healthy || die "предыдущий образ тоже не прошёл healthcheck; смотрите docker compose logs $SERVICE"
    log "откат выполнен, работает предыдущая версия"
}

show_logs() {
    docker compose ps || true
    docker compose logs --tail=100 "$SERVICE" || true
}

# --- сценарии --------------------------------------------------------------

run_check() {
    preflight_common
    preflight_deploy
    build_image
    verify_image
    log "проверка завершена: образ собирается и проходит проверки безопасности"
    log "работающий контейнер не изменялся"
}

run_deploy() {
    preflight_common
    preflight_deploy
    backup_access_db
    build_image
    verify_image

    if ! start_bot || ! wait_healthy; then
        show_logs
        rollback
        die "деплой не удался, выполнен откат"
    fi

    docker compose ps
    log "деплой завершён: работает $IMAGE"
}

run_rollback() {
    preflight_common
    [[ -f .env ]] || die "нет .env"
    docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 \
        || die "нет образа $ROLLBACK_IMAGE"
    rollback
    docker compose ps
}

exec 9>"$LOCK_FILE"
flock -n 9 || die "уже выполняется другой деплой ($LOCK_FILE)"

case "$MODE" in
    check) run_check ;;
    deploy) run_deploy ;;
    rollback) run_rollback ;;
esac
