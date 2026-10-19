# CLAUDE.md — контекст проекта Order Queue Bot

> Этот файл помогает агенту быстро войти в контекст, не перечитывая весь репозиторий.
> Источник правды по функциональности — `README.md` (он подробный). Здесь — сжатая
> карта проекта, команды, ключевые потоки и «грабли».

## Что это

Бот держит в активной очереди N заявок на выплату на сайте `spark-trading.cash`:
следит за очередью, мгновенно «забирает» заявки (POST формы `take_payout`), довзятие
через периодический опрос. Управление — из Telegram (`/run`, `/stop`, `/watch`,
`/status`, проброс 2FA). Поддерживает несколько кабинетов (аккаунтов) параллельно.

Главная цель оптимизаций — **минимизировать задержку обнаружения заявки**. Поэтому
горячий цикл работает на `httpx` (без браузера), а Playwright используется только для
разовой авторизации (получить cookies в `auth.json`).

## Быстрые команды

```bash
# зависимости (локально)
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && playwright install chromium

# запуск
cp .env.example .env      # заполнить
python -m app.main        # или: docker compose up --build

# тесты (офлайн, из корня проекта)
python -m unittest discover -s tests -t .
python -m unittest tests.test_browser_auth -v

# утилиты
python tools/check_accounts.py    # какие кабинеты прочитались, пути сессий
python tools/metrics_report.py    # отчёт по ./data/metrics.jsonl

# деплой на Hostinger (см. README → «Деплой»)
./deploy.sh
```

## Карта кода (`app/`)

| файл | роль |
|---|---|
| `main.py` | точка входа: config → bot app → по воркеру на каждый кабинет (`notify`/`notify_file` прокидываются сюда) |
| `config.py` | чтение env → `Config`/`Account`; парсинг админов и мультиаккаунта |
| `state.py` | `WorkerState` (по одному на кабинет) + `BotState` (общий `state`); делегирует старые однокабинетные атрибуты в #1 |
| `telegram_bot.py` | обработчики команд, диалоги выбора кабинета, маршрутизация 2FA |
| `http_worker.py` | **ГОРЯЧИЙ ЦИКЛ** на httpx: `run_worker`, `take_loop`, `watch_loop`, `PayoutClient`, парсер HTML, `Metrics` |
| `browser_worker.py` | Playwright: разовая авторизация (`login_via_browser`, `ensure_logged_in`) + старый browser-цикл (fallback) |

## Поток: от Telegram до заявки

1. `main.py` строит `Application`, запускает polling и по одному `run_worker(cfg, account, ws, notify, notify_file)` на кабинет (задача живёт постоянно).
2. Админ шлёт `/run` (с выбором кабинета) → `telegram_bot.cmd_run` ставит `ws.running=True`, `ws.mode="take"`.
3. `http_worker.run_worker` видит `ws.running` → `ensure_session` (cookies из `auth.json`; если протухли — разовая браузерная авторизация) → `PayoutClient`.
4. `take_loop`: `TAKE_PARALLEL_POLLERS` воронок делают `GET QUEUE_URL`, парсят HTML регулярками (`parse_rows`), и одним «такт»-ом делают `POST` формы `take_payout`, не блокируя цикл.
5. `demands_loop`/`resync_demands_loop` раз в `DEMANDS_SYNC_S` сверяются со страницей `DEMANDS_URL` — это **источник правды о занятых слотах** (автовыдача, заявки с прошлого запуска).

### Превентивное взятие будущих id (`PREDICTIVE_TAKE`)

ID заявок на площадке идут по порядку, поэтому отдельный фон `predict_loop` шагает
вперёд **окнами** по `PREDICTIVE_TAKE_COUNT` (по умолчанию **25**): от курсора
`ws.last_order_id` шлёт `take_payout` на `cursor+1 … cursor+N`, **не дожидаясь их
появления в HTML**. Если окно целиком неудачное — курсор сдвигается на наибольший id
окна и шлётся следующее окно (поиск вперёд) до предела `PREDICTIVE_TAKE_LOOKAHEAD`, а
затем **возвращается к якорю и обстреливает диапазон заново** (retry).
`_observe_queue` (в `_poll_loop`) обновляет общий для страницы `csrf_token`
(`ws.last_csrf_token`) и якорь `ws.observed_anchor` (макс. реально увиденный id); дедуп —
по `inflight` / `demands_ids` / `taken_order_ids` / `ws.predictive_sent`. Окно НЕ
ограничивается свободными слотами: площадка сама не даст держать в работе больше своего
лимита (≈10), а несуществующие id просто не подтвердятся. Пауза между окнами —
`PREDICTIVE_TAKE_GAP_MS`. Такие взятия идут с `predicted=True`: без дампов (если не
`PREDICTIVE_TAKE_DUMPS=true`) и без уведомлений о неудаче. Видимость предсказательных POST:
лог `🔮 превентивно: окно #a…#b`, при `DEBUG=true` — короткое уведомление в Telegram
`«🔮 Превентивно: N POST take_payout (окно #a…#b, якорь #c)»`. Шлётся **один раз на
каждый новый реально увиденный id (якорь)**, не чаще `PREDICTIVE_NOTIFY_MIN_S=3с`:
раньше текст строился из неизменного `observed_anchor` и сообщение дублировалось каждые
3 секунды, пока очередь стоит на месте. В `metrics.jsonl` — поле `"predicted": true`.
По умолчанию выключено (`PREDICTIVE_TAKE=false`) — это доп. POST-нагрузка.

**Горячий GET-цикл активен всегда.** `_poll_loop` берёт паузу из `_poll_wait()` →
`TAKE_POLL_MIN/MAX_MS` (при предиктиве — тоже), а `_effective_pollers` = все
`TAKE_PARALLEL_POLLERS`. Предиктив лишь ДОБАВЛЯЕТ спекулятивные POST, не заменяя опрос:
именно активный GET находит реальные заявки и обновляет якорь/CSRF. Опциональный
`PREDICTIVE_TAKE_POLL_MS`: если он БОЛЬШЕ `TAKE_POLL_MAX_MS` — «тихий» режим (редкий
опрос + одна воронка); если меньше (напр. `1` мс) — наоборот, агрессивнее, воронки все.
По умолчанию `0` (активный цикл по `TAKE_POLL_*`).

**Защита от «убегания вперёд»** (чтобы всегда оставаться в актуальном цикле):
- спекулятивные (предсказанные) отправки идут в `ws.predicted_inflight` и **не считаются
  занятыми слотами** (`slots_taken(..., speculative=...)`) — поэтому `_poll_loop` продолжает
  опрашивать реальную очередь и не «слепнет» во время пачки предсказаний;
- `predict_loop` **не предсказывает, когда реальная очередь полна** (нет свободного слота);
- если реальность обогнала курсор (`observed_anchor > last_order_id`) — курсор **догоняет**
  её (re-anchor), чтобы не долбить неактуальные id;
- **сброс «пула» от пойманной заявки**: как только в очереди видна новая реальная
  заявка (её id больше прежнего якоря), `_observe_queue` ставит курсор на её id и
  чистит `predictive_sent` — следующее окно POST идёт на `id+1…` (а не от убежавшего
  вперёд предсказанного курсора);
- верхняя граница забегания — `PREDICTIVE_TAKE_LOOKAHEAD` id от `observed_anchor`
  (0 = без ограничения; меньше значение = безопаснее). **Дойдя до границы, курсор
  возвращается к якорю и диапазон прогоняется снова** (`ws.predictive_sent`
  очищается) — ключ к поимке: id, который пробовали раньше (когда заявки ещё не было),
  пробуется ПОВТОРНО. Без этого «прочёс вперёд» давал лишь одну попытку на id, и заявки
  терялись («пропускаем все заявки»).

### Разбор ответа сервера на взятие (`_classify_take_response`)

Ответ на `POST take_payout` сохраняется в дамп (`_dump_take_request` → `response.text_head`
+ `server_reply`) и разбирается, чтобы ОЗВУЧИТЬ результат в Telegram:
- `TAKE_REPLY_TAKEN` — сервер подтвердил взятие (маркер из `TAKE_SUCCESS_MARKERS`
  или строка заявки в ответе имеет статус «в работе у провайдера»);
- `TAKE_REPLY_GONE` — заявки нет: по маркеру `TAKE_GONE_MARKERS` ИЛИ структурно
  (это страница очереди — есть `payout-table`/`payout-status`/`payout-card`, — но
  строки с нашим id в ней уже нет; значит забрали или её и не было для предсказанного id);
- `TAKE_REPLY_STILL_AVAILABLE` — форма «Забрать выплату» по этому id всё ещё в ответе;
- `TAKE_REPLY_UNKNOWN` — ответ вообще не похож на страницу очереди (например,
  редирект на дашборд `/account/`) — по телу понять нельзя.

⚠️ Это **диагностика**, а не решение: наша ли заявка — по-прежнему определяет сверка со
страницей demands (`_confirm_taken`). Маркеры задаются в env только если формулировки
площадки известны (`TAKE_SUCCESS_MARKERS` / `TAKE_GONE_MARKERS`, по умолчанию пусто).

**Повтор POST при неоднозначном ответе.** Если классификация вернула `still_available`
или `unknown`, а `_confirm_taken` взятие НЕ подтвердил, `_do_take` ограниченно повторяет
`POST take_payout` (`TAKE_RETRY_COUNT`, по умолчанию 2; пауза `TAKE_RETRY_DELAY_MS`).
Для `still_available` confirm делается быстрым (`attempts=1`, без задержки), чтобы не
ждать ~1.6 с. `taken`/`gone` не повторяются, предсказанные id тоже (иначе POST-шторм).
Каждая попытка пишется в `metrics.jsonl` с полем `attempt`.

## Источник правды о размере очереди

`TARGET_QUEUE_SIZE` считается НЕ по памяти бота, а по странице `DEMANDS_URL`:
слот занят, если статус заявки — «в работе у провайдера» (`STATUS_IN_WORK_MARKER`).
Память (`taken_order_ids`) живёт только до рестарта, а часть заявок площадка выдаёт
сама. См. `WorkerState.slots_taken`, `ws.demands_ids`.

## Авторизация (только в browser_worker)

- Пробуем открыть `QUEUE_URL`; если сессия жива — редиректа на `/signin` не будет.
- Иначе: форма входа (`auth-form`) → логин/пароль → при необходимости 2FA через Telegram → сохранение `auth.json`.
- Пережидаются Cloudflare-проверки (`_is_challenge_page`, `_wait_challenge_passed`) и «дребезг» URL (`_wait_url_settle`).
- Интерактивную Cloudflare Turnstile «Verify you are human» (managed challenge,
  `cType: 'managed'`) бот **нажимает сам**: `_click_turnstile()` ищет чекбокс,
  перебирая **все кадры** (`page.frames`, в т.ч. вложенные iframe) с URL
  `challenges.cloudflare.com`/`challenge-platform` или `blob:`/`about:blank`,
  затем fallback через `frame_locator` (`iframe[id^='cf-chl-widget-']`) и
  `#challenge-stage` для challenge без iframe. Клик — best-effort (force-клик при
  перекрытии), один раз за проход. Вызывается из `_wait_for_login_form` и
  `_wait_challenge_passed` на каждой итерации ожидания.
- При блокировке HTTP-цикла (403/антибот, Cloudflare) воркер **сам сбрасывает
  cookies кабинета и авторизуется заново**: циклы ставят `ws.needs_reauth`, а
  `run_worker` вызывает `_recover_from_block` (удаляет `auth.json` через
  `_reset_stored_session`, затем `ensure_session(force_reauth=True)`). Число подряд
  переавторизаций ограничено `MAX_BLOCK_REAUTH` (env `BLOCK_REAUTH_ATTEMPTS`, по умолчанию 3).
- 🚨 **ГРАБЛИ (уже исправлялась)**: после успешного входа сайт часто уводит на дашборд
  `/account/` («личный кабинет», класс `user-account-page`), где нет таблицы выплат.
  `_looks_like_cabinet()` обязан считать такой дашборд кабинетом, иначе успешный вход
  помечается как неудача (`debug_after_login_unexpected`), сессия не сохраняется и
  горячий цикл не стартует. Маркер `user_dashboard` НЕ подходит — он есть и на
  странице входа (в путях ассетов).
- 🚨 **ГРАБЛИ (уже исправлялась)**: после отправки формы входа сайт показал
  Cloudflare-проверку «Один момент…» (видно в дампе `debug_after_login_unexpected_*`).
  Финальная проверка `ensure_logged_in` теперь распознаёт challenge (`_is_challenge_page`),
  пережидает его (`_wait_challenge_passed`) и только потом решает про успех; вход при этом
  НЕ считается неудачей, сессия сохраняется. Если после проверки снова `/signin` — это
  честный провал (`after_login`).

## Мультиаккаунт

- Кабинет #1: `SITE_LOGIN`/`SITE_PASSWORD`/`SITE_NAME`, сессия `STORAGE_STATE_PATH` (`./data/auth.json`).
- Кабинет N: `SITE_LOGIN_N`/`SITE_PASSWORD_N`/`SITE_NAME_N`, сессия `./data/auth_N.json`.
- Авторизации и 2FA идут строго по одной (`_login_lock`), горячие циклы — параллельны.
- `WORKER_BACKEND=browser` поддерживает только кабинет #1.

## Переменные окружения (ключевые)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` (+ `TELEGRAM_ADMIN_IDS`/`SUB_USER`…),
`SITE_LOGIN[_N]`, `SITE_PASSWORD[_N]`, `SITE_NAME[_N]`, `SITE_URL`, `QUEUE_URL`,
`DEMANDS_URL`, `TARGET_QUEUE_SIZE`, `RELOAD_MIN_MS/MAX_MS`, `TAKE_POLL_MIN_MS/MAX_MS`,
`TAKE_PARALLEL_POLLERS`, `DEMANDS_SYNC_S`, `LOGIN_FORM_TIMEOUT_S`, `WORKER_BACKEND`
(`http`|`browser`), `HTTP_TIMEOUT_S`, `DEBUG_CAPTURE_ATTEMPTS`, `HEADLESS`,
`BLOCK_REAUTH_ATTEMPTS`, `PREDICTIVE_TAKE`, `PREDICTIVE_TAKE_COUNT`,
`PREDICTIVE_TAKE_GAP_MS`, `PREDICTIVE_TAKE_LOOKAHEAD`, `PREDICTIVE_TAKE_POLL_MS`, `PREDICTIVE_TAKE_DUMPS`, `DEBUG`,
`TAKE_SUCCESS_MARKERS`, `TAKE_GONE_MARKERS`, `STORAGE_STATE_PATH`, `LOG_PATH`, `METRICS_PATH`.
Полный список и комментарии — `.env.example`.

> `load_dotenv()` **не перезаписывает** уже заданные env-переменные. Залипшие
> `export SITE_LOGIN_2=…` из терминала перебьют `.env` — снимайте `unset`/чистый терминал
> (см. `tools/check_accounts.py`).

## Тесты

- `tests/test_browser_auth.py` — логика авторизации без браузера (fake Page/Context): `_looks_like_*`, `ensure_logged_in`.
- `tests/test_http_worker.py` — парсер строк, `PayoutClient`, учёт слотов demands (часть тестов на реальных дампах из `./data`, иначе `skipUnless`).
- `tests/test_config.py` — env → Config/Account/админы.
- `tests/test_telegram_flow.py` — обработчики команд.
Все тесты офлайновые; сеть не нужна.

## Конвенции и «грабли»

- Комментарии/строки/докстринги — на русском, как в проекте. Сообщения Telegram — с префиксом кабинета `[Имя] …`.
- HTML парсится **регулярками** (`http_worker`), без BeautifulSoup/DOM.
- Задержку обнаружения дают: пауза между опросами + RTT; POST уходит фоновой задачей.
- Дампы при ошибках — в `./data/debug_*.html|png` (+ версии с timestamp). Смотреть сюда при разборе «что показал сайт».
- Дампы/логи/сессии в `./data` — в `.gitignore`; чистить `./deploy.sh prune`.
- `.env` и `./data/auth*.json` = живые секреты, в git не коммитить.
- Изменения логики авторизации обязательно покрывать тестами в `test_browser_auth.py`.
