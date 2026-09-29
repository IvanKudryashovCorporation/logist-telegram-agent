"""Отчёт о качестве разбора заявок — те же цифры, что в админке ``/admin``.

    python -m scripts.parse_stats
    python -m scripts.parse_stats --days 30

Отвечает на вопросы владельца без запуска сайта: действительно ли LLM разбирает
заявки, какая доля заказов приходит неполной, сколько токенов уходит и не молчит
ли агент (очередь ``failed``). Источник данных общий с админкой —
:mod:`app.services.reporting`, поэтому цифры не расходятся.
"""

import argparse
import asyncio

from app.parsing.llm_parser import cache_stats
from app.services import reporting


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


async def main(days: int) -> None:
    parse = await reporting.parse_summary(days=days)
    orders = await reporting.order_status_breakdown()
    queue = await reporting.queue_summary()
    cache = cache_stats()

    print(f"=== Разбор сообщений за {days} дн. ===")
    print(f"  обработано сообщений:      {parse['processed']}")
    print(f"  обращений к LLM:           {parse['llm_calls']}")
    print(f"  отсечено предфильтром:     {parse['prefiltered']}  (экономия {_percent(parse['prefilter_saving'])})")
    print(f"  найдено заявок:            {parse['orders_found']}")
    print(f"  неполных полей:            {parse['missing_fields']}  (доля {_percent(parse['incomplete_rate'])})")
    print(f"  ошибки + очередь:          {parse['errors']}")
    print(f"  токены prompt/completion:  {parse['prompt_tokens']} / {parse['completion_tokens']}")
    print(f"  токенов всего:             {parse['total_tokens']}")
    print(f"  средняя задержка LLM:      {parse['avg_latency_ms']} мс")

    print("\n  По исходам:")
    for outcome, count in parse["by_outcome"].items():
        share = (count / parse["processed"]) if parse["processed"] else 0.0
        print(f"    {outcome:14} {count:6}  {_percent(share)}")

    print("\n=== Заказы ===")
    print(f"  всего:                     {orders['total']}")
    print(f"  взято водителями:          {orders['taken']}")
    print(f"  с жалобами водителей:      {orders['with_problems']}")
    for value, count in orders["by_status"].items():
        label = orders["status_labels"].get(value, value)
        print(f"    {label:28} {count}")

    print("\n=== Очередь повторного разбора ===")
    for value, count in queue.items():
        marker = "  <-- нужен человек" if value == "failed" and count else ""
        print(f"  {value:10} {count}{marker}")

    print("\n=== Кэш разбора (в памяти процесса) ===")
    total_lookups = cache["hits"] + cache["misses"]
    hit_rate = (cache["hits"] / total_lookups) if total_lookups else 0.0
    print(f"  hits/misses/size: {cache['hits']}/{cache['misses']}/{cache['size']}  (попаданий {_percent(hit_rate)})")

    if not parse["processed"]:
        print(
            "\nСтатистики нет: агент не разбирал сообщения за период "
            "или выключен PARSE_STATS_ENABLED."
        )
    elif parse["incomplete_rate"] > 0.2:
        print(
            "\nВнимание: доля неполных полей выше 20% — водители видят заявки "
            "без цены/времени. Проверьте промпт и модель (LLM_MODEL)."
        )
    if queue.get("failed"):
        print(
            f"\nВнимание: {queue['failed']} сообщений не разобрано после всех попыток. "
            "Устраните причину и выполните: python -m scripts.retry_queue --run"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="период статистики в днях")
    args = parser.parse_args()
    asyncio.run(main(args.days))
