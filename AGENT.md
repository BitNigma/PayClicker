# AGENT.md — рабочие инструкции для агента

Спутник `CLAUDE.md` (там — карта проекта). Здесь — как действовать: инварианты,
типовые задачи, где искать причины, как проверять результат.

## TL;DR

- Проект: Telegram-бот, который держит N заявок в активной очереди на `spark-trading.cash`.
- Горячий цикл — `app/http_worker.py` (httpx, без браузера). Браузер — только разовая
  авторизация в `app/browser_worker.py`.
- Тесты офлайн: `python -m unittest discover -s tests -t .` (запускать из корня).
- Язык кода/комментариев/логов — русский. Не переписывать на английский.
- Правки логики — минимальные, в стиле окружающего кода; новые тесты только в `tests/`.

## Инварианты (не нарушать)

1. **Размер очереди берётся с площадки** (`DEMANDS_URL`), а не из памяти. Память живёт
   до рестарта; часть заявок сайт выдаёт сам (автовыдача). См. `WorkerState.demands_ids`,
   `slots_taken`, `resync_required`.
2. **Горячий цикл не использует браузер/DOM.** Parsing — только регулярки.
3. **POST взятия не блокирует опрос** — уходит отдельной задачей; цикл сразу делает
   следующий GET.
4. **Один `WorkerState` на кабинет.** Старые однокабинетные обращения к `state.*`
   делегируются кабинету #1 (`BotState.__getattr__`) — не ломать делегирование.
5. **Авторизации/2FA сериализуются** (`browser_worker._login_lock`); горячие циклы — параллельны.

## Типовые задачи

### Добавить/поправить Telegram-команду
Файл `app/telegram_bot.py`: обработчик → `application.add_handler(CommandHandler(...))`
в `build_bot_app`. Проверить `_check_chat` (только админы). Тест — `tests/test_telegram_flow.py`.

### Изменить опрос/взятие заявок
Файл `app/http_worker.py`: `take_loop`, `_poll_loop`, `PayoutClient`, `parse_rows`.
Замеры времени — в `Metrics` (`metrics.jsonl`). Не добавлять синхронных блокировок в цикл.

### Правки авторизации (браузер)
Файл `app/browser_worker.py`: `ensure_logged_in`, `login_via_browser`, `_looks_like_*`.
**Обязательно** добавить/поправить тест в `tests/test_browser_auth.py` (fake Page/Context,
сеть не нужна).

### Новая env-переменная
Добавить чтение в `app/config.py` (`Config`), значение по умолчанию с запасом, и
прокомментировать в `.env.example`; при необходимости — тест в `tests/test_config.py`.

## Диагностика: где искать причину

| Симптом | Куда смотреть |
|---|---|
| Вход «прошёл», но цикл не стартует | `./data/debug_after_login_unexpected_*.html` → если это дашборд `/account/` (`user-account-page`), значит `_looks_like_cabinet()` не распознал входную страницу |
| Капча/антибот | маркеры в `UNEXPECTED_PAGE_MARKERS`, `_is_challenge_page`; дампы `debug_*captcha/challenge*` |
| «Форма входа не появилась» | `debug_login_form_missing_*.html`; селекторы `SEL_LOGIN_*`, таймаут `LOGIN_FORM_TIMEOUT_S` |
| Заявки не берутся, хотя сессия жива | `./data/metrics.jsonl` (`saw_to_sent_ms`, `poll_rtt_ms`, `submit_rtt_ms`); `tools/metrics_report.py` |
| Счётчик очереди расходится | сверка `DEMANDS_URL`, `resync_demand*s`, `/reset_session` |
| Мультиаккаунт читается не так | `python tools/check_accounts.py` (залипшие env!) |

Дампы и логи можно читать прямо из `./data` (`read_files`), картинки `.png` — тоже
поддерживаются. Имена дампов содержат причину и timestamp.

## Проверка изменений (definition of done)

1. `python -m unittest discover -s tests -t .` — всё зелёное.
2. Для правок авторизации/парсинга — есть тест, воспроизводящий баг (падал бы до фикса).
3. Логика согласована с `README.md`; при изменении поведения — обновить README/CLAUDE.md/AGENT.md.
4. Ничего из `./data` (сессии, дампы, `metrics.jsonl`) и `.env` не попадает в git.

## Быстрая проверка окружения

```bash
python tools/check_accounts.py         # кабинеты, сессии, бэкенд
python -m app.main                     # запуск (нужен валидный .env)
```
