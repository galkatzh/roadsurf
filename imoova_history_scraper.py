#!/usr/bin/env python3
"""Fetch Imoova's full relocation history and pre-aggregate it for the
history visualization app (history.html).

The GraphQL API returns deals of every status (READY, ARCHIVED, EXPIRED,
SOLD_OUT, ...) when no status filter is passed, which is effectively the
site's historical record. We fetch a lean field set page by page and
aggregate client-side into a compact JSON:

  {
    "meta":     { fetched_at, total_records, months: [...] },
    "cities":   { id: {name, state, region, lat, lng} },
    "routes":   { "fromId>toId": { "YYYY-MM": count } },
    "vehicles": { vehicleType: { "YYYY-MM": count } },
    "deal_types": { RELOCATION|GAP_RENTAL: { "YYYY-MM": count } }
  }

Pickup/dropoff per-city-per-month counts are derived from `routes` by the
app, so the file stays small while everything is filterable by date AND
location. A deal's month is its earliest departure date (falling back to
available-from, then creation date).

Usage:
    uv run --with requests imoova_history_scraper.py [-o imoova_history.json]
        [--page-size 500] [--delay 0.15] [--max-pages N]
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

API = "https://api.imoova.com/graphql"

# NOTE: earliest_departure_date is clamped server-side to max(today, ...),
# so it is useless for history — available_from_date holds the real date.
# Explicit CREATED_AT ordering keeps pagination stable while we walk pages.
QUERY = """
query RelocationList($first: Int!, $page: Int) {
  relocations(first: $first, page: $page,
              orderBy: [{column: CREATED_AT, order: ASC}]) {
    paginatorInfo { total currentPage lastPage }
    data {
      id type status created_at
      available_from_date latest_departure_date
      departureCity { id name state region lat lng }
      deliveryCity { id name state region lat lng }
      vehicle { type }
    }
  }
}
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": "https://www.imoova.com",
    "Referer": "https://www.imoova.com/",
}


def fetch_page(session, page, page_size):
    body = {
        "operationName": "RelocationList",
        "query": QUERY,
        "variables": {"first": page_size, "page": page},
    }
    for attempt in range(1, 5):
        try:
            resp = session.post(API + "?RelocationList", json=body, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise RuntimeError(json.dumps(payload["errors"])[:300])
            return payload["data"]["relocations"]
        except (requests.RequestException, ValueError, KeyError,
                RuntimeError) as err:
            if attempt == 4:
                raise
            print(f"  page {page}: retry {attempt} after: {err}",
                  file=sys.stderr)
            time.sleep(2 * attempt)


MONTH_MIN = "2020-01"
MONTH_MAX = f"{datetime.now().year + 2}-12"   # drop garbage far-future dates


def month_of(rec):
    date = (rec.get("available_from_date")
            or rec.get("latest_departure_date")
            or rec.get("created_at") or "")
    month = date[:7] if len(date) >= 7 else None
    if month and MONTH_MIN <= month <= MONTH_MAX:
        return month
    return None


def main():
    ap = argparse.ArgumentParser(description="Aggregate Imoova history")
    ap.add_argument("-o", "--output", default="imoova_history.json")
    ap.add_argument("--page-size", type=int, default=200,
                    help="max 200 (server-enforced)")
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    cities = {}
    routes = defaultdict(lambda: defaultdict(int))
    vehicles = defaultdict(lambda: defaultdict(int))
    deal_types = defaultdict(lambda: defaultdict(int))
    records = 0
    skipped = 0

    page, last_page = 1, 1
    while page <= last_page:
        chunk = fetch_page(session, page, args.page_size)
        info = chunk["paginatorInfo"]
        last_page = info["lastPage"]
        if args.max_pages:
            last_page = min(last_page, args.max_pages)
        if page == 1 or page % 10 == 0 or page == last_page:
            print(f"page {page}/{last_page} ({info['total']} records total)")

        for rec in chunk["data"]:
            month = month_of(rec)
            dep, dst = rec.get("departureCity"), rec.get("deliveryCity")
            if not (month and dep and dst):
                skipped += 1
                continue
            for c in (dep, dst):
                if c["id"] not in cities:
                    cities[c["id"]] = {
                        "name": c.get("name"), "state": c.get("state"),
                        "region": c.get("region"),
                        "lat": c.get("lat"), "lng": c.get("lng"),
                    }
            routes[f"{dep['id']}>{dst['id']}"][month] += 1
            vtype = (rec.get("vehicle") or {}).get("type") or "UNKNOWN"
            vehicles[vtype][month] += 1
            deal_types[rec.get("type") or "UNKNOWN"][month] += 1
            records += 1

        page += 1
        time.sleep(args.delay)

    months = sorted({m for r in routes.values() for m in r})
    result = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "https://www.imoova.com/relocations (GraphQL history)",
            "total_records": records,
            "skipped_records": skipped,
            "city_count": len(cities),
            "route_count": len(routes),
            "first_month": months[0] if months else None,
            "last_month": months[-1] if months else None,
        },
        "cities": cities,
        "routes": {k: dict(v) for k, v in routes.items()},
        "vehicles": {k: dict(v) for k, v in vehicles.items()},
        "deal_types": {k: dict(v) for k, v in deal_types.items()},
    }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, separators=(",", ":"))
    size_kb = len(json.dumps(result)) // 1024
    print(f"\n{records} records ({skipped} skipped) -> {args.output} "
          f"(~{size_kb} KB, {len(cities)} cities, {len(routes)} routes, "
          f"{months[0] if months else '?'}..{months[-1] if months else '?'})")


if __name__ == "__main__":
    main()
