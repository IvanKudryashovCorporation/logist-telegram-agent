"""Ручная правка геокодера: какие места не нашлись и задать им координаты.

    python -m scripts.geo_places missing
    python -m scripts.geo_places set "<название как в заявке>" <lat> <lon>

``set`` записывает координаты в кэш со статусом manual (геокодер их больше не
трогает) и сбрасывает отметку «проверено» у заказов без координат — фоновый
воркер агента пересчитает их на ближайшем проходе.
"""

import argparse
import asyncio

from sqlalchemy import func, or_, select, update

from app import geo
from app.db.base import SessionLocal
from app.models import GeoPlace, Order
from app.models.geo_place import SOURCE_MANUAL, STATUS_MANUAL, STATUS_NOT_FOUND
from app.timeutil import now_utc_naive


async def show_missing() -> None:
    async with SessionLocal() as session:
        not_found = (
            await session.execute(select(GeoPlace).where(GeoPlace.status == STATUS_NOT_FOUND))
        ).scalars().all()
        print(f"Геокодер не нашёл ({len(not_found)}):")
        for place in not_found:
            print(f"  {place.key}")

        for column, label in ((Order.from_city, "откуда"), (Order.to_city, "куда")):
            lat = Order.from_lat if column is Order.from_city else Order.to_lat
            rows = (
                await session.execute(
                    select(column, func.count())
                    .where(Order.geo_checked_at.is_not(None), column.is_not(None), lat.is_(None))
                    .group_by(column)
                    .order_by(func.count().desc())
                )
            ).all()
            print(f"\nЗаказы с городом «{label}» без координат ({len(rows)} названий):")
            for city, count in rows:
                print(f"  {count:>4}  {city}")


async def set_place(name: str, lat: float, lon: float) -> None:
    clean, hint = geo.normalize_place(name)
    if not clean:
        print("Пустое название.")
        return
    key = geo.place_key(clean, hint)
    candidate = {"lat": lat, "lon": lon, "name": name, "kind": "manual"}

    async with SessionLocal() as session:
        place = (
            await session.execute(select(GeoPlace).where(GeoPlace.key == key))
        ).scalar_one_or_none()
        if place is None:
            place = GeoPlace(key=key)
            session.add(place)
        place.candidates = [candidate]
        place.status = STATUS_MANUAL
        place.source = SOURCE_MANUAL
        place.checked_at = now_utc_naive()

        reset = await session.execute(
            update(Order)
            .where(
                Order.geo_checked_at.is_not(None),
                or_(Order.from_lat.is_(None), Order.to_lat.is_(None)),
            )
            .values(geo_checked_at=None, updated_at=Order.updated_at)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    print(f"Сохранено: {key} -> ({lat}, {lon}). К пересчёту отправлено заказов: {reset.rowcount}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("missing")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("name")
    set_parser.add_argument("lat", type=float)
    set_parser.add_argument("lon", type=float)
    args = parser.parse_args()

    if args.command == "missing":
        asyncio.run(show_missing())
    else:
        asyncio.run(set_place(args.name, args.lat, args.lon))


if __name__ == "__main__":
    main()
