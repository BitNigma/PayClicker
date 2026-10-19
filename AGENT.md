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
| Вход «прошёл», но цикл не стартует | `./data/debug_after_login_unexpected_*.html` → если это дашборд `/account/` (`user-account-page`), значит `_looks_like_cabinet()` не распознал входную страницу; если это Cloudflare «Один момент…» — `ensure_logged_in` пережидает проверку (`_wait_challenge_passed`) и сохраняет сессию (грабля: успешный вход помечался неудачей) |
| Капча/антибот | маркеры в `UNEXPECTED_PAGE_MARKERS`, `_is_challenge_page`; дампы `debug_*captcha/challenge*` |
| HTTP 403 / «Just a moment…» в горячем цикле | `_classify()` (`_BLOCKED_MARKERS`) + `PayoutClient._check()` → `BlockedError`; воркер сам сбрасывает cookies (`_reset_stored_session`) и переавторизуется (`_recover_from_block`), лимит `BLOCK_REAUTH_ATTEMPTS` (по умолч. 3) |
| Cloudflare просит «Verify you are human» | `_click_turnstile()` — обход всех `page.frames` (домен Cloudflare / `blob:`), затем `iframe[id^='cf-chl-widget-']` + `#challenge-stage`; вызывается из `_wait_for_login_form`/`_wait_challenge_passed` |
| «Форма входа не появилась» | `debug_login_form_missing_*.html`; селекторы `SEL_LOGIN_*`, таймаут `LOGIN_FORM_TIMEOUT_S` |
| Заявки не берутся, хотя сессия жива | `./data/metrics.jsonl` (`saw_to_sent_ms`, `poll_rtt_ms`, `submit_rtt_ms`); `tools/metrics_report.py` |
| Предиктив «слепнет» / убегает вперёд | `predict_loop` + `_poll_loop`: спекулятивные отправки в `ws.predicted_inflight` не считаются слотами; при полной очереди предсказатель ждёт; курсор догоняет `observed_anchor`; предел — `PREDICTIVE_TAKE_LOOKAHEAD` |
| Много GET → бан Cloudflare | по умолчанию горячий цикл активен (`TAKE_POLL_MIN/MAX_MS` × `TAKE_PARALLEL_POLLERS`). Чтобы снизить нагрузку, задайте `PREDICTIVE_TAKE_POLL_MS` БОЛЬШЕ `TAKE_POLL_MAX_MS` (напр. 800–1500): опрос станет редким и воронка одна (`_poll_wait`/`_effective_pollers`). Значение МЕНЬШЕ (напр. 1) — наоборот, агрессивнее, воронки все |
| Постоянные 403 / бан по IP | задайте `PROXY_URLS`/`PROXY_FILE` (+`PROXY_ROTATE_ON_BLOCK=true`): при блоке бот меняет прокси, не трогая cookies (`ProxyPool`, `ws.proxy_rotations`). Файл прокси перечитывается на лету (`load_proxies`). Тест `ProxySupportTest` |
| Пропускаем заявки (хотя предиктив включён) | `predict_loop`: дойдя до `PREDICTIVE_TAKE_LOOKAHEAD`, курсор ВОЗВРАЩАЕТСЯ к якорю и диапазон прогоняется заново (retry) — иначе id пробуется лишь раз, и заявка, чей id появился позже, теряется. Тест `test_predict_loop_does_not_run_past_lookahead`. Проверьте, что `PREDICTIVE_TAKE_LOOKAHEAD>0` (иначе повторов нет) и `PREDICTIVE_TAKE_GAP_MS` не слишком большой |
| Заявку не берём, хотя она ещё доступна (`still_available`/`unknown`) | `_do_take` ограниченно повторяет POST (`TAKE_RETRY_COUNT`, `TAKE_RETRY_DELAY_MS`), если `_confirm_taken` не подтвердил. `taken`/`gone` и предсказанные id не повторяются. В `metrics.jsonl` — поле `attempt`. Тест `TakeRetryTest` |
| Взяли заявку или нет — что ответил сервер | `_classify_take_response()` по телу `POST take_payout` (`TAKE_REPLY_*`); дамп `./data/take_request_http_*.json` (`text_head` до `TAKE_DUMP_BODY_CHARS` + `server_reply`); ground truth всё равно `_confirm_taken` по demands. `unknown` = ответ не похож на страницу очереди (редирект на дашборд) |
| «Не вижу предиктивных POST» | они есть, но по умолчанию не дампятся и молчат. Ищите в `./data/metrics.jsonl` записи с `"predicted": true` (и `poll_rtt_ms=null`), строку лога `🔮 превентивно: окно …`; дампы — `PREDICTIVE_TAKE_DUMPS=true` |
| «🔮 Превентивно…» дублируется в Telegram | так было раньше: сообщение слалось раз в `PREDICTIVE_NOTIFY_MIN_S` (3с) с неизменным якорем (`observed_anchor`). Теперь — один раз на НОВЫЙ якорь и с реальным окном `#a…#b`. Тишина: `DEBUG=false` |
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
