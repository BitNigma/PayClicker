"""Отчёт по замерам горячего цикла из data/metrics.jsonl (JSONL).

Отвечает на вопрос «стоит ли переписывать горячий цикл на Go»: показывает,
сколько времени уходит на клиент (`saw_to_sent_ms`, `py_overhead_ms`,
`parse_ms`) и сколько — на сеть и частоту опроса (`poll_rtt_ms`,
`submit_rtt_ms`, `since_prev_get_ms`), а также статистику подтверждений
(`event=take_result`).

Запуск из корня проекта (интерпретатором проекта, Python 3.10+):

    python tools/metrics_report.py                  # путь по умолчанию
    python tools/metrics_report.py path/to/metrics.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

DEFAULT_PATH = Path("data/metrics.jsonl")

TAKE_FIELDS = (
    "saw_to_sent_ms",
    "py_overhead_ms",
    "parse_ms",
    "poll_rtt_ms",
    "since_prev_get_ms",
    "submit_rtt_ms",
)


def pct(values: list, p: float):
    """Перцентиль (линейная интерполяция) или None для пустого списка."""
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    frac = k - lo
    return round(values[lo] + (values[hi] - values[lo]) * frac, 2)


def summarize(name: str, values: list) -> str:
    if not values:
        return f"{name:<18} нет данных"
    return (
        f"{name:<18} n={len(values):<4} мин={min(values):>8.2f} "
        f"p50={pct(values, 50):>8.2f} p90={pct(values, 90):>8.2f} "
        f"p99={pct(values, 99):>8.2f} макс={max(values):>9.2f} "
        f"сред={st.mean(values):>8.2f}"
    )


def read_records(path: Path) -> tuple:
    """Читает JSONL и раскладывает записи на event=take / event=take_result."""
    takes, results = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # обрезанная строка (процесс убили посреди записи)
        if rec.get("event") == "take":
            takes.append(rec)
        elif rec.get("event") == "take_result":
            results.append(rec)
    return takes, results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(DEFAULT_PATH))
    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"нет файла замеров: {path}")

    takes, results = read_records(path)
    print(f"Файл: {path}")
    print(f"Записей event=take: {len(takes)}, event=take_result: {len(results)}")
    if takes:
        ts = [r.get("ts", 0) for r in takes]
        print(
            "Период: "
            f"{dt.datetime.fromtimestamp(min(ts)):%Y-%m-%d %H:%M} .. "
            f"{dt.datetime.fromtimestamp(max(ts)):%Y-%m-%d %H:%M}"
        )

    print("\n=== ВСЕ КАБИНЕТЫ (мс) ===")
    for field in TAKE_FIELDS:
        print(summarize(field, [r[field] for r in takes
                                if isinstance(r.get(field), (int, float))]))

    print("\n=== ПО КАБИНЕТАМ ===")
    by_acc = defaultdict(list)
    for rec in takes:
        by_acc[rec.get("account") or rec.get("account_index") or "?"].append(rec)
    for acc in sorted(by_acc, key=str):
        rows = by_acc[acc]
        saw = [r["saw_to_sent_ms"] for r in rows if "saw_to_sent_ms" in r]
        py = [r["py_overhead_ms"] for r in rows if "py_overhead_ms" in r]
        poll = [r["since_prev_get_ms"] for r in rows
                if isinstance(r.get("since_prev_get_ms"), (int, float))]
        print(
            f"{acc:<14} take={len(rows):<4} "
            f"saw_to_sent p50={pct(saw, 50)} p90={pct(saw, 90)} макс={max(saw) if saw else None} | "
            f"py_overhead p50={pct(py, 50)} | "
            f"окно_опроса p50={pct(poll, 50)}"
        )

    print("\n=== РЕЗУЛЬТАТ (взяли / упустили) ===")
    if results:
        ok = [r for r in results if r.get("confirmed")]
        lost = [r for r in results if not r.get("confirmed")]
        print(f"подтверждено: {len(ok)} / {len(results)} "
              f"({100.0 * len(ok) / len(results):.1f}%)")
        for label, rows in (("взяли", ok), ("упустили", lost)):
            ms = [r["confirm_ms"] for r in rows
                  if isinstance(r.get("confirm_ms"), (int, float))]
            if ms:
                print(summarize(f"confirm_ms({label})", ms))
        for rec in lost[:3]:
            print("  пример упущенной:", json.dumps(rec, ensure_ascii=False))

    print("\n=== HTTP-СТАТУСЫ POST ===")
    statuses = defaultdict(int)
    for rec in takes:
        statuses[rec.get("http_status")] += 1
    print(dict(statuses))


if __name__ == "__main__":
    main()
