#!/usr/bin/env bash
# Развёртывание order-bot на VPS (Hostinger и любой другой Linux) через Docker Compose.
#
# НА СЕРВЕРЕ (из каталога проекта):
#   ./deploy.sh                собрать образ, проверить .env, запустить контейнер
#   ./deploy.sh update         обновить код (git pull) + пересобрать + перезапустить
#   ./deploy.sh logs           логи контейнера в реальном времени
#   ./deploy.sh status         состояние контейнера + хвост actions.log
#   ./deploy.sh restart|stop   перезапустить / остановить
#   ./deploy.sh check          проверить .env и кабинеты (внутри контейнера)
#   ./deploy.sh test           прогнать офлайн-тесты (внутри контейнера)
#   ./deploy.sh prune [дней]   удалить старые debug-дампы из /data (по умолчанию 7)
#
# С ЛОКАЛЬНОЙ МАШИНЫ (когда проект ещё не на сервере):
#   ./deploy.sh push root@IP [каталог-на-сервере]   залить проект по ssh и запустить
#     PUSH_ENV=0        не заливать .env (по умолчанию 1 — заливать)
#     PUSH_SESSIONS=1   залить и data/auth*.json — тогда логин/2FA на сервере не нужны
#
#   ./deploy.sh help     эта справка
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SERVICE="order-bot"       # имя сервиса в docker-compose.yml
IMG="order-bot:latest"    # тег образа (image: в docker-compose.yml)

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose "$@"; }

usage() {
    cat <<'EOF'
Развёртывание order-bot в Docker (VPS / Hostinger)

  На сервере:
    ./deploy.sh                собрать образ, проверить .env, запустить контейнер
    ./deploy.sh update         обновить код (git pull) + пересобрать + перезапустить
    ./deploy.sh logs           логи контейнера в реальном времени
    ./deploy.sh status         состояние контейнера + хвост actions.log
    ./deploy.sh restart|stop   перезапустить / остановить
    ./deploy.sh check          проверить .env и кабинеты (внутри контейнера)
    ./deploy.sh test           прогнать офлайн-тесты (внутри контейнера)
    ./deploy.sh prune [дней]   удалить debug-дампы из /data старше N дней (по умолчанию 7)

  С локальной машины:
    ./deploy.sh push root@IP [каталог]   залить проект по ssh и запустить на сервере
      PUSH_ENV=0        не заливать .env (по умолчанию заливается)
      PUSH_SESSIONS=1   залить и data/auth*.json (повторный логин/2FA не нужны)

  Пример:
    ssh root@1.2.3.4 'command -v docker || curl -fsSL https://get.docker.com | sh'
    PUSH_SESSIONS=1 ./deploy.sh push root@1.2.3.4 /root/order-bot
EOF
}

# --- служебное ---------------------------------------------------------------

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        die "Docker не установлен. Поставьте: curl -fsSL https://get.docker.com | sh"
    fi
    if ! docker info >/dev/null 2>&1; then
        die "Docker-демон не отвечает. Запустите: sudo systemctl start docker"
    fi
    if ! docker compose version >/dev/null 2>&1; then
        die "Нет плагина «docker compose» (Compose v2). Поставьте пакет docker-compose-plugin."
    fi
}

# .env нужен даже для тестов: docker-compose.yml подключает его как env_file.
ensure_env_file() {
    if [ -f .env ]; then
        return 0
    fi
    if [ ! -f .env.example ]; then
        die "Нет ни .env, ни .env.example — запускайте скрипт из корня проекта."
    fi
    cp .env.example .env
    warn "Файл .env создан из .env.example — впишите в него свои значения."
}

# Проверка, что .env реально заполнен (для команд, которые запускают бота).
require_env_filled() {
    ensure_env_file
    local missing="" key
    for key in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_ID SITE_LOGIN SITE_PASSWORD; do
        if ! grep -qE "^${key}=.+" .env; then
            missing="$missing $key"
        fi
    done
    if [ -n "$missing" ]; then
        die "В .env не заполнено:$missing
Откройте его (nano .env), впишите свои значения и запустите ./deploy.sh снова."
    fi
    if grep -q '123456:ABC-DEF' .env; then
        die "В .env остался пример токена — вставьте настоящий токен от @BotFather."
    fi
}

container_running() {
    local cid
    cid="$(compose ps -q "$SERVICE" 2>/dev/null || true)"
    if [ -z "$cid" ]; then
        return 1
    fi
    [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ]
}

ensure_image() {
    if ! docker image inspect "$IMG" >/dev/null 2>&1; then
        log "Образ $IMG ещё не собран — собираю…"
        compose build
    fi
}

# Выполнить команду в образе: в живом контейнере — exec, иначе разовый run.
# $1 — интерпретатор (python|sh), дальше — его аргументы (можно добавить ENV=value).
run_in_image() {
    local interp="$1"
    shift
    if container_running; then
        compose exec -T "$SERVICE" "$interp" "$@"
    else
        ensure_image
        compose run --rm --entrypoint "$interp" "$SERVICE" "$@"
    fi
}

# --- команды -----------------------------------------------------------------

cmd_up() {
    require_docker
    require_env_filled
    log "Собираю образ (первый раз это несколько минут: базовый образ Playwright ~2 ГБ)…"
    compose build
    log "Проверяю конфигурацию кабинетов…"
    run_in_image python tools/check_accounts.py
    log "Запускаю контейнер…"
    compose up -d
    sleep 3
    compose ps
    printf '\n'
    compose logs --tail 15 || true
    cat <<'NEXT'

Готово. Дальше всё делается из Telegram:
  1) /start_session  — бот залогинится и попросит 2FA-код (отправьте его следующим сообщением);
  2) /run            — выберите кабинет(ы) и начните взятие заявок;
  3) /status         — сколько заявок в работе сейчас.

Логи:  ./deploy.sh logs   (или docker compose logs -f)
Стоп:  ./deploy.sh stop
Файлы сессий лежат в ./data (volume) и переживают пересборку — повторный логин не нужен.
NEXT
}

cmd_check() {
    require_docker
    require_env_filled
    run_in_image python tools/check_accounts.py
}

cmd_test() {
    require_docker
    ensure_env_file
    run_in_image python -m unittest discover -s tests -t .
}

cmd_logs() {
    require_docker
    compose logs -f --tail 100
}

cmd_status() {
    require_docker
    compose ps
    printf '\n'
    if container_running; then
        log "Последние строки /data/actions.log:"
        compose exec -T "$SERVICE" tail -n 15 /data/actions.log || true
    else
        warn "Контейнер не запущен."
    fi
}

cmd_restart() {
    require_docker
    compose restart
    compose ps
}

cmd_stop() {
    require_docker
    compose stop
}

cmd_update() {
    require_docker
    require_env_filled
    if [ -d .git ]; then
        log "Обновляю код: git pull…"
        git pull --ff-only || warn "git pull не удался — собираю то, что уже есть в каталоге."
    else
        warn "Каталога .git нет: код обновляется копированием файлов (./deploy.sh push с локальной машины)."
    fi
    log "Пересобираю и перезапускаю…"
    compose build
    compose up -d
    compose ps
}

# Чистка debug-дампов: без неё /data на сервере растёт с каждой попыткой взятия.
cmd_prune() {
    require_docker
    local days="${1:-7}"
    case "$days" in
        ''|*[!0-9]*) die "Число дней должно быть целым (например: ./deploy.sh prune 14)." ;;
    esac
    log "Удаляю debug-дампы старше $days дней из /data…"
    run_in_image sh -c "find /data -maxdepth 1 -name 'attempt_*' -type f -mtime +${days} -delete; echo '  дампов старше ${days} дн. больше нет'; du -sh /data"
}

# С локальной машины: залить проект по ssh и запустить его там.
cmd_push() {
    local target="${1:-}" remote_dir="${2:-/root/order-bot}"
    if [ -z "$target" ]; then
        die "Укажите сервер: ./deploy.sh push root@1.2.3.4 [каталог-на-сервере]"
    fi
    if ! command -v rsync >/dev/null 2>&1; then
        die "Локально нужен rsync (на macOS есть из коробки; на Linux: apt install rsync)."
    fi
    if ! command -v ssh >/dev/null 2>&1; then
        die "Локально нужен ssh."
    fi

    local push_env="${PUSH_ENV:-1}"
    if [ "$push_env" = "1" ]; then
        require_env_filled
    else
        warn "PUSH_ENV=0 — .env не заливаю: на сервере он должен быть уже заполнен."
    fi

    log "Готовлю каталог $remote_dir на $target…"
    ssh "$target" "mkdir -p '$remote_dir'"

    local opts=(-az --delete -e ssh
        --exclude '.git/'
        --exclude '.venv/'
        --exclude '__pycache__/'
        --exclude '*.pyc'
        --exclude '.vscode/'
        --exclude '.DS_Store'
        --exclude 'data/')
    if [ "$push_env" = "1" ]; then
        log "Переношу .env (секреты идут по ssh-каналу)."
    else
        opts+=(--exclude '.env')
    fi

    log "Копирую файлы проекта…"
    rsync "${opts[@]}" ./ "$target:$remote_dir/"

    if [ "${PUSH_SESSIONS:-0}" = "1" ]; then
        if compgen -G "data/auth*.json" >/dev/null; then
            log "Переношу файлы сессий (data/auth*.json)…"
            ssh "$target" "mkdir -p '$remote_dir/data'"
            rsync -az -e ssh data/auth*.json "$target:$remote_dir/data/"
        else
            warn "Локально нет data/auth*.json — на сервере потребуется /start_session."
        fi
    fi

    log "Собираю и запускаю на сервере…"
    ssh "$target" "cd '$remote_dir' && bash ./deploy.sh"
}

# --- точка входа -------------------------------------------------------------

main() {
    local cmd="${1:-up}"
    case "$cmd" in
        up|deploy)       cmd_up ;;
        update)          cmd_update ;;
        check)           cmd_check ;;
        test|tests)      cmd_test ;;
        logs|log)        cmd_logs ;;
        status|ps)       cmd_status ;;
        restart)         cmd_restart ;;
        stop|down)       cmd_stop ;;
        build)           require_docker; compose build ;;
        prune|clean)     shift; cmd_prune "$@" ;;
        push)            shift; cmd_push "$@" ;;
        help|-h|--help)  usage ;;
        *)               die "Неизвестная команда «$cmd». Справка: ./deploy.sh help" ;;
    esac
}

main "$@"

