"""Состояние бота: по одному WorkerState на каждый кабинет (аккаунт) + диалог с админами.

Раньше состояние было одним-единственным (один кабинет). Теперь воркеров столько,
сколько кабинетов в конфиге: у каждого свой running/mode/logged_in/taken_order_ids
и своя очередь для 2FA-кода. Телеграм-бот адресует команды конкретному кабинету.
"""
import asyncio
from dataclasses import dataclass, field

_PRIMARY_INDEX = 1


@dataclass
class WorkerState:
    """Состояние ОДНОГО кабинета. На каждый кабинет запускается свой воркер."""

    index: int = 0
    name: str = ""
    running: bool = False
    # "watch" — только уведомлять о появлении заявок; "take" — брать заявки
    mode: str = "watch"
    logged_in: bool = False
    awaiting_2fa: bool = False
    last_status: str = "idle"
    taken_count: int = 0
    taken_order_ids: set = field(default_factory=set)
    # Заявки, которые прямо сейчас занимают слот на площадке (страница demands):
    # и взятые нами, и выданные сайтом автоматически, и оставшиеся с прошлого
    # запуска процесса. Источник правды о размере активной очереди — площадка,
    # а не память: при рестарте память пуста, а заявки в работе остаются.
    demands_ids: set = field(default_factory=set)
    snapshot_requested: bool = False
    # /reset_session поставил True: кэш очереди очищен, воркер обязан сделать
    # свежую сверку со страницей demands, ПРЕЖДЕ чем продолжать брать заявки
    # (иначе на пустом кэше он наберёт лишнее сверх TARGET_QUEUE_SIZE).
    resync_required: bool = False

    # Канал, через который телеграм-бот прокидывает введённый 2FA-код в воркер
    two_fa_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    async def wait_for_2fa_code(self, timeout: int = 300) -> str:
        self.awaiting_2fa = True
        try:
            return await asyncio.wait_for(self.two_fa_queue.get(), timeout=timeout)
        finally:
            self.awaiting_2fa = False

    def submit_2fa_code(self, code: str) -> None:
        self.two_fa_queue.put_nowait(code)

    def slots_taken(self, inflight=()) -> int:
        """Сколько слотов целевой очереди занято прямо сейчас.

        Объединяем три источника: заявки «в работе» на площадке (`demands_ids`),
        взятые в этой сессии (`taken_order_ids`) и отправленные, но ещё не
        подтверждённые (`inflight`). Дедуп по id, поэтому счёт корректен и когда
        сверка со страницей demands недоступна (тогда работаем по памяти).

        Пока идёт ресинхронизация после /reset_session — возвращаем 0, чтобы
        воркер не начал брать заявки на основании пустого (ещё не сверенного)
        кэша: слоты всё равно посчитаются заново при первой свежей сверке.
        """
        if self.resync_required:
            return 0
        return len(self.demands_ids | self.taken_order_ids | set(inflight))

    def reset_queue_cache(self) -> None:
        """Полная очистка локального кэша очереди кабинета (команда /reset_session).

        Стираем ТОЛЬКО память бота: взятые этой сессией заявки, сверку со
        страницей demands и счётчик. Cookies/сессию сайта (auth.json) не трогаем —
        это не «разлогин», а пересчёт активной очереди с нуля.

        Если воркер сейчас работает — выставляем флаг resync_required, под которым
        цикл обязан ПЕРЕД продолжением сделать свежую сверку с площадкой.
        """
        self.taken_order_ids.clear()
        self.demands_ids.clear()
        self.taken_count = 0
        if self.running:
            self.resync_required = True


@dataclass
class BotState:
    """Общее состояние бота: воркеры по кабинетам + незавершённые диалоги в чатах."""

    workers: dict = field(default_factory=dict)        # index -> WorkerState
    chat_pending: dict = field(default_factory=dict)   # chat_id -> {"action", "step", ...}

    def get(self, index: int) -> WorkerState:
        return self.workers[index]

    def ensure(self, index: int, name: str = "") -> WorkerState:
        ws = self.workers.get(index)
        if ws is None:
            ws = WorkerState(index=index, name=name or f"Кабинет {index}")
            self.workers[index] = ws
        return ws

    # ------------------------------------------------------------------
    # Совместимость со старым однокабинетным кодом (browser_worker обращается
    # к атрибутам напрямую: state.running, state.taken_order_ids и т.п.).
    # Такие обращения делегируются первому кабинету (#1), поэтому браузерный
    # бэкенд продолжает работать как есть — но только для кабинета #1.
    # ------------------------------------------------------------------
    _DELEGATED = (
        "running",
        "mode",
        "logged_in",
        "awaiting_2fa",
        "last_status",
        "taken_count",
        "taken_order_ids",
        "demands_ids",
        "snapshot_requested",
        "resync_required",
        "two_fa_queue",
    )

    def _primary(self) -> WorkerState:
        return self.ensure(_PRIMARY_INDEX)

    def __getattr__(self, item):
        if item in BotState._DELEGATED:
            return getattr(self._primary(), item)
        raise AttributeError(item)

    def __setattr__(self, key, value):
        if key in BotState._DELEGATED and "workers" in self.__dict__:
            setattr(self._primary(), key, value)
        else:
            object.__setattr__(self, key, value)

    async def wait_for_2fa_code(self, timeout: int = 300) -> str:
        return await self._primary().wait_for_2fa_code(timeout)

    def submit_2fa_code(self, code: str) -> None:
        self._primary().submit_2fa_code(code)


state = BotState()
