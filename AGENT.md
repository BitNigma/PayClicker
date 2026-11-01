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

### Подключить другую площадку / SPA-таблицу выплат
Код править НЕ нужно: разметка и эндпоинт взятия вынесены в `Config`
(`app/config.py`, дефолты = `spark-trading.cash`), значения задаются env —
см. `CLAUDE.md` → «Профиль площадки» и `LOCAL_RUN.md` (разделы 6 и 6a).
Порядок:
1. `python -m app.main --check` — убедиться, что конфиг читается без ошибок.
2. Заполнить `SITE_HOST`, `SIGNIN_PATH`, `QUEUE_URL`, `DEMANDS_URL`,
   `PAYOUT_ROW_MARKERS`, `STATUS_AVAILABLE_MARKERS`/`STATUS_IN_WORK_MARKERS`/
   `STATUS_DONE_MARKERS`, `QUEUE_PAGE_MARKERS`, `EMPTY_ROW_TEXT`.
2a. Вход: режим бот определяет САМ по наличию поля `#email` — есть поле, значит
   3-шаговый SPA (Email → «Далее» → Password → «Войти» → 2FA → «Войти»), иначе
   обычная серверная форма. Нестандартные id/тексты кнопок задаются через
   `SPA_EMAIL_SELECTOR`, `SPA_PASSWORD_SELECTOR`, `SPA_NEXT_SELECTOR`,
   `SPA_SUBMIT_SELECTOR`, `SPA_TWOFA_INPUT_SELECTOR`. Тесты:
   `test_spa_3step_login_without_2fa` / `test_spa_3step_login_with_2fa`
   в `tests/test_browser_auth.py`.
3. SPA (таблица рисуется JS, id — текстом в первой ячейке, статус — plain text,
   кнопка взятия шлёт JSON): `ROW_ID_FROM_FIRST_CELL=true`,
   `TAKE_REQUIRES_CSRF=false`, `TAKE_PAYLOAD_FORMAT=json`,
   `QUEUE_PAGE_MARKERS=payouts-compact`, **`CONFIRM_REQUIRES_IN_WORK=true`**
   (иначе бот «подтвердит» и свободные заявки — таблица показывает все).
3a. SPA на Laravel + Inertia (911pay): очередь — JSON `props.payouts.data`
   (в теле XHR или в `data-page` обычного GET), id — `uuid`, статус — код
   (`unassigned`/`pending`/`wait_admin`/`returned_to_trader`/`success`/`canceled`/`expired`),
   взятие — `POST /trader/payouts/{uuid}/claim` **без тела**. Настройки:
   `TAKE_PAYLOAD_FORMAT=none`, `TAKE_API_URL_TEMPLATE=…/{id}/claim`,
   `TAKE_CSRF_HEADER=X-XSRF-TOKEN`, `TAKE_CSRF_COOKIE=XSRF-TOKEN`,
   `QUEUE_JSON_ID_FIELD=uuid`, `QUEUE_JSON_STATUS_FIELD=status`,
   `TAKE_GONE_MARKERS=срок выплаты истёк`. `parse_rows()` распознаёт Inertia
   сам и переводит код статуса в текст (`_STATUS_CODE_LABELS`). При
   `CONFIRM_REQUIRES_IN_WORK=true` подтверждение — ТОЛЬКО статус «в работе»
   (`_is_in_work_status`): `expired`/`canceled` в demands НЕ считаются взятием.
   Тесты — `tests/test_inertia_spa.py`.
4. Прогнать тесты `SpaPlatformTest` в `tests/test_http_worker.py` (и
   `tests/test_inertia_spa.py` для Inertia) и снять дамп первой же страницы
   очереди (`DEBUG_CAPTURE_ATTEMPTS=true`), чтобы сверить маркеры с реальной
   разметкой.

### Узнать реальный запрос взятия (неизвестный API площадки)
Пока запрос взятия неизвестен, HTTP-цикл взять заявку не может. Инструмент
`app/take_capture.py` поднимает **видимый** Chromium с уже сохранённой сессией,
сам обновляет страницу очереди, кликает кнопку взятия и перехватывает реальный
запрос/ответ сервера:
- `python -m app.take_capture` — 1 взятие, потом окно остаётся открытым;
- `--max-takes 0` — непрерывно брать кликом (страховочный режим без API);
- `--no-click` — только обновлять/смотреть, ничего не нажимая;
- `--headless`, `--refresh 3`, `--click-wait 2.5`, `--login-wait 300`, `--out DIR`.

Дампы — `./data/take_capture/take_<id>_<ts>.json` (+HTML/PNG; JSON с правами 600 —
внутри cookie/токены). В консоль печатаются готовые `TAKE_API_URL_TEMPLATE`,
`TAKE_PAYLOAD_FORMAT`, `TAKE_ID_FIELD`, `TAKE_REQUIRES_CSRF` и curl. Если сессия
протухла, инструмент не закрывает окно, а ждёт ручной вход. Чистые функции
(сборка рецепта, id из строки, маскирование секретов, cookie) покрыты
`tests/test_take_capture.py`.

ВАЖНО: перед запуском остановите бота в Telegram (`/stop`) — иначе инструмент и
HTTP-цикл будут конкурировать за одни и те же заявки.

### Правки авторизации (браузер)
Файл `app/browser_worker.py`: `ensure_logged_in`, `login_via_browser`, `_looks_like_*`.
**Обязательно** добавить/поправить тест в `tests/test_browser_auth.py` (fake Page/Context,
сеть не нужна).

**3-шаговый SPA-вход** (Email → Password → 2FA) обслуживает `_fill_login_credentials`:
режим выбирается по фактической разметке — есть поле `#email`, значит SPA
(`#email` → «Далее» → `#password` → «Войти»), иначе старая серверная форма
(email и пароль сразу). После пароля `ensure_logged_in` проверяет поле кода
(`#twofa-code`) ДО URL — SPA может остаться на том же роуте. Селекторы — в
`Config` (`spa_email_selector`, `spa_password_selector`, `spa_next_selector`,
`spa_submit_selector`, `spa_twofa_input_selector`), переопределяются через env
`SPA_*_SELECTOR`. Правя этот поток, обновляйте `test_spa_3step_login_without_2fa`
и `test_spa_3step_login_with_2fa`.

⚠️ **Главная ловушка SPA: оба шага уже в DOM, неактивный спрятан через
`display:none`.** На 911pay `#password` присутствует в разметке с самого начала,
хотя шаг пароля ещё не показан. Проверка по НАЛИЧИЮ поля (`query_selector`)
считала шаг активным → бот пропускал «Далее» → падал по таймауту на заполнении
скрытого поля → браузер закрывался («заполняет email, не жмёт Далее, потом
закрывается»). Поэтому:

* для решения «показан ли шаг» используйте `_is_visible_any()` (`element.is_visible()`),
  а НЕ `_query_any()` — наличие в DOM ничего не значит;
* кнопка «Далее» бывает `disabled=""` до валидации email, поэтому клик идёт через
  `_click_next_step()`: Playwright ждёт снятия `disabled`, а если кнопка так и
  осталась выключенной — фолбэк на Enter в поле email (кнопка `type="submit"`
  внутри `<form>`, Enter отправляет ту же форму).
  Регрессия: `test_spa_password_in_dom_but_hidden_still_clicks_next`.

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
| После входа окно браузера исчезает, кнопку взятия нажать негде | так и задумано у HTTP-цикла: `login_via_browser` сохраняет cookies в `auth.json` и закрывает Chromium. Для живого окна с автообновлением, кликом и перехватом запроса — `python -m app.take_capture` (см. «Узнать реальный запрос взятия»); дампы в `./data/take_capture/` |
| `take_capture` не видит кнопку взятия | кнопка ищется по ТЕКСТУ (не по классу): добавьте её подпись в `--button-text "…,…"` или в `PAYOUT_TAKE_BUTTON_MARKERS`; при `--no-click` сохраняется `./data/take_capture/watch_*.html` для подбора разметки |
| Заявки не берутся, хотя сессия жива | `./data/metrics.jsonl` (`saw_to_sent_ms`, `poll_rtt_ms`, `submit_rtt_ms`); `tools/metrics_report.py` |
| Предиктив «слепнет» / убегает вперёд | `predict_loop` + `_poll_loop`: спекулятивные отправки в `ws.predicted_inflight` не считаются слотами; при полной очереди предсказатель ждёт; курсор догоняет `observed_anchor`; предел — `PREDICTIVE_TAKE_LOOKAHEAD` |
| Много GET → бан Cloudflare | по умолчанию горячий цикл активен (`TAKE_POLL_MIN/MAX_MS` × `TAKE_PARALLEL_POLLERS`). Чтобы снизить нагрузку, задайте `PREDICTIVE_TAKE_POLL_MS` БОЛЬШЕ `TAKE_POLL_MAX_MS` (напр. 800–1500): опрос станет редким и воронка одна (`_poll_wait`/`_effective_pollers`). Значение МЕНЬШЕ (напр. 1) — наоборот, агрессивнее, воронки все |
| Постоянные 403 / бан по IP | задайте `PROXY_URLS`/`PROXY_FILE` (+`PROXY_ROTATE_ON_BLOCK=true`): при блоке бот меняет прокси, не трогая cookies (`ProxyPool`, `ws.proxy_rotations`). Файл прокси перечитывается на лету (`load_proxies`). Тест `ProxySupportTest` |
| Пропускаем заявки (хотя предиктив включён) | `predict_loop`: дойдя до `PREDICTIVE_TAKE_LOOKAHEAD`, курсор ВОЗВРАЩАЕТСЯ к якорю и диапазон прогоняется заново (retry) — иначе id пробуется лишь раз, и заявка, чей id появился позже, теряется. Тест `test_predict_loop_does_not_run_past_lookahead`. Проверьте, что `PREDICTIVE_TAKE_LOOKAHEAD>0` (иначе повторов нет) и `PREDICTIVE_TAKE_GAP_MS` не слишком большой |
| Заявку не берём, хотя она ещё доступна (`still_available`/`unknown`) | `_do_take` ограниченно повторяет POST (`TAKE_RETRY_COUNT`, `TAKE_RETRY_DELAY_MS`), если `_confirm_taken` не подтвердил. `taken`/`gone` и предсказанные id не повторяются. В `metrics.jsonl` — поле `attempt`. Тест `TakeRetryTest` |
| Сервер отвечает 422 `{"message":"Срок выплаты истёк — возьмите другую"}` | это НЕ ошибка формата запроса: заявку уже забрали/оффер истёк. `_classify_take_response` разбирает JSON-поле `message` и по `TAKE_GONE_MARKERS=срок выплаты истёк` даёт `gone`; повтор POST не делается (`TAKE_RETRY_COUNT=0`) |
| Взяли заявку или нет — что ответил сервер | `_classify_take_response()` по телу `POST take_payout` (`TAKE_REPLY_*`); дамп `./data/take_request_http_*.json` (`text_head` до `TAKE_DUMP_BODY_CHARS` + `server_reply`); ground truth всё равно `_confirm_taken` по demands, **кроме предсказанных id с ответом `gone`** (тогда `confirmed=False` без GET — иначе 63k предсказанных POST дают ~126k лишних GET). `unknown` = ответ не похож на страницу очереди (редирект на дашборд) |
| «Не вижу предиктивных POST» | они есть, но по умолчанию не дампятся и молчат. Ищите в `./data/metrics.jsonl` записи с `"predicted": true` (и `poll_rtt_ms=null`), строку лога `🔮 превентивно: окно …`; дампы — `PREDICTIVE_TAKE_DUMPS=true` |
| «🔮 Превентивно…» дублируется в Telegram | так было раньше: сообщение слалось раз в `PREDICTIVE_NOTIFY_MIN_S` (3с) с неизменным якорем (`observed_anchor`). Теперь — один раз на НОВЫЙ якорь и с реальным окном `#a…#b`. Тишина: `DEBUG=false` |
| Счётчик очереди расходится | сверка `DEMANDS_URL`, `resync_demand*s`, `/reset_session` |
| Заявки «берутся», но на самом деле чужие/нераспределённые | площадка показывает в demands ВСЕ заявки (SPA): подтверждение шло по факту строки с id. Включите `CONFIRM_REQUIRES_IN_WORK=true` — `_confirm_taken` примет заявку только со статусом «в работе/завершена» (`status_in_work_markers`/`status_done_markers`). Тест `SpaPlatformTest.test_confirm_requires_in_work_rejects_available_row` |
| SPA: строки таблицы не находятся, id пустой | id лежит текстом в первой ячейке `<td>`, а не в скрытом поле/«# 123». Нужны `ROW_ID_FROM_FIRST_CELL=true` + `PAYOUT_ROW_MARKERS`/`STATUS_*_MARKERS` под разметку (`parse_rows`, `_first_td_text`). Тест `test_parses_row_with_id_in_first_cell` |
| SPA: POST взятия не проходит (нет csrf / сервер не понял тело) | `TAKE_REQUIRES_CSRF=false`, `TAKE_PAYLOAD_FORMAT=json`, при необходимости `TAKE_API_URL_TEMPLATE` с `{id}`, пустые `TAKE_CSRF_FIELD`/`TAKE_ACCESS_KEY_FIELD`/`TAKE_ACT_FIELD` = «поле не отправлять» (`_take_payload`). Ответ API разбирает `_classify_take_response` (`{"success":true}`/`{"ok":1}`/`{"taken":true}`) |
| SPA: вход не проходит (3 шага Email → Password → 2FA) | `_fill_login_credentials` сам выбирает режим по наличию `#email`: SPA идёт `#email` → «Далее» → `#password` → «Войти», затем 2FA `#twofa-code` → «Войти». Если у площадки другие id/тексты кнопок — задайте `SPA_EMAIL_SELECTOR`/`SPA_PASSWORD_SELECTOR`/`SPA_NEXT_SELECTOR`/`SPA_SUBMIT_SELECTOR`/`SPA_TWOFA_INPUT_SELECTOR`. Тесты `test_spa_3step_login_without_2fa`/`test_spa_3step_login_with_2fa` в `tests/test_browser_auth.py` |
| SPA: после пароля бот не ждёт код 2FA | Шаг кода проверяется ДО URL (`query_selector(#twofa-code)`): SPA может остаться на том же роуте `/signin` и показывать форму кода. Если шаг пропускается — сверьте `SPA_TWOFA_INPUT_SELECTOR` с реальным id поля |
| Поле email заполняется, но «Далее» не нажимается и браузер закрывается | Кнопка «Далее» на 911pay отрисована `disabled=""` и включается только после валидации email по `blur`/`change`, а `page.fill()` шлёт лишь `input` — Playwright ждёт снятия `disabled` до таймаута, клик не происходит. Бот снимает фокус (`_blur_active()`) перед кликом, затем `_click_next_step()`: 5 с → Enter → снятие `disabled` в DOM. То же для «Войти». Если всё равно не проходит — проверьте `SPA_NEXT_SELECTOR`/`SPA_SUBMIT_SELECTOR` по дампу `debug_login_page_dump_*.html` |
| Вход «проходит», но сессия не работает (сразу редирект на `/login`) | `_on_signin()` жёстко искал `/signin`, а 911pay использует `/login`: бот считал, что уже ушёл со входа, и сохранял нерабочую сессию как успешную. Путь задаётся `SIGNIN_PATH` (`/login` у 911pay) |
| Взяли заявку или нет — что ответил сервер (JSON API) | `_classify_take_response()` понимает и HTML-маркеры (`TAKE_SUCCESS_MARKERS`/`TAKE_GONE_MARKERS`), и JSON: `{"success":true}` / `{"ok":1}` / `{"taken":true}` → `taken`; текст ошибки из `TAKE_GONE_MARKERS` → `gone` |
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
python -m app.main --check             # конфиг + импорты, без бота и без сети
python -m app.main                     # запуск (нужен валидный .env)
```

Две «страшные» ошибки на старте — не крэш:

- `ImportError: attempted relative import with no known parent package` — файл
  запускали как `python app/main.py`; точка входа это переживает (сама правит
  `sys.path`), но правильный запуск — `python -m app.main` из корня.
- `telegram.error.Conflict: terminated by other getUpdates request` — уже
  работает второй экземпляр бота (локальный запуск + контейнер). Процесс жив,
  но апдейты забирает другой; остановите лишний (`pgrep -fl app.main`,
  `docker compose ps`). Конфликт логируется одной строкой через `error_callback`.
