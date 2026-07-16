#!/usr/bin/env python3
"""Scraper for Imoova vehicle relocation deals (https://www.imoova.com/relocations).

Imoova is a RedwoodJS app backed by an open GraphQL API at
https://api.imoova.com/graphql (no auth; arbitrary queries are accepted,
introspection is disabled). The list page uses the `RelocationList` operation.

Enums (extracted from the app bundles):
  Region:           AU NZ US CA EU JP TW PI SACU SA ME MX KR
  RelocationStatus: READY DRAFT PAUSED EXPIRED SOLD_OUT ARCHIVED
  RelocationType:   RELOCATION GAP_RENTAL

Region slugs used by the website subdirectories (/relocations/<slug>):
  australia new-zealand usa canada europe japan taiwan pacific-islands
  south-africa south-america middle-east mexico south-korea

A deal's page is  https://www.imoova.com/relocations/deal/{deal_slug}

Money fields (hire_unit_rate, retail_rate, ...) are in minor currency units
(cents); the scraper converts them to major units.

Usage:
    uv run --with requests imoova_scraper.py [-o imoova_relocations.json]
        [--region europe] [--status READY] [--page-size 100]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

API = "https://api.imoova.com/graphql"
SITE = "https://www.imoova.com"

REGION_SLUGS = {
    "AU": "australia", "NZ": "new-zealand", "US": "usa", "CA": "canada",
    "EU": "europe", "JP": "japan", "TW": "taiwan", "PI": "pacific-islands",
    "SACU": "south-africa", "SA": "south-america", "ME": "middle-east",
    "MX": "mexico", "KR": "south-korea",
}
SLUG_TO_REGION = {v: k for k, v in REGION_SLUGS.items()}

QUERY = """
query RelocationList($first: Int!, $page: Int, $regions: [Region!], $status: [RelocationStatus!]) {
  relocations(first: $first, page: $page, regions: $regions, status: $status) {
    paginatorInfo { total currentPage lastPage }
    data {
      id reference deal_slug name status type count
      available_from_date available_to_date
      earliest_departure_date latest_departure_date
      currency hire_unit_type hire_unit_rate hire_units_allowed
      extra_hire_units_allowed extra_hire_unit_rate retail_rate
      booking_fee_amount measurement distance_allowed
      is_ferry_required minimum_hire_units
      departureCity { id name slug state region lat lng }
      deliveryCity { id name slug state region lat lng }
      vehicle {
        id type name brand model code seatbelts sleeps fuel transmission
        is_self_contained has_kitchen fallback_image_url
      }
      supplier { id name }
      images(limit: 1) { url }
      inclusions { type value description has_free_tank }
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
    "Origin": SITE,
    "Referer": SITE + "/",
}


def money(minor, currency):
    if minor is None:
        return None
    return {"amount": minor / 100.0, "currency": currency}


def slim_city(c):
    if not c:
        return None
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "state": c.get("state"),
        "region": c.get("region"),
        "region_slug": REGION_SLUGS.get(c.get("region")),
        "latitude": c.get("lat"),
        "longitude": c.get("lng"),
    }


def fetch_page(session, page, page_size, regions, statuses):
    body = {
        "operationName": "RelocationList",
        "query": QUERY,
        "variables": {
            "first": page_size,
            "page": page,
            "regions": regions,
            "status": statuses,
        },
    }
    if regions is None:
        del body["variables"]["regions"]
    for attempt in range(1, 4):
        try:
            resp = session.post(API + "?RelocationList", json=body, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise RuntimeError(json.dumps(payload["errors"])[:300])
            return payload["data"]["relocations"]
        except (requests.RequestException, ValueError, KeyError) as err:
            if attempt == 3:
                raise
            print(f"  retry {attempt} after error: {err}", file=sys.stderr)
            time.sleep(2 * attempt)


def main():
    ap = argparse.ArgumentParser(description="Scrape Imoova relocation deals")
    ap.add_argument("-o", "--output", default="imoova_relocations.json")
    ap.add_argument("--region", default=None,
                    help="region slug (e.g. europe, middle-east) or enum (EU); "
                         "default: all regions")
    ap.add_argument("--status", default="READY",
                    help="comma-separated RelocationStatus values (default READY)")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args()

    regions = None
    if args.region:
        key = args.region.strip()
        region = SLUG_TO_REGION.get(key.lower()) or key.upper()
        if region not in REGION_SLUGS:
            sys.exit(f"Unknown region '{args.region}'. "
                     f"Use one of: {', '.join(sorted(SLUG_TO_REGION))}")
        regions = [region]

    statuses = [s.strip().upper() for s in args.status.split(",") if s.strip()]

    session = requests.Session()
    session.headers.update(HEADERS)

    deals = []
    page, last_page = 1, 1
    while page <= last_page:
        chunk = fetch_page(session, page, args.page_size, regions, statuses)
        info = chunk["paginatorInfo"]
        last_page = info["lastPage"]
        print(f"page {page}/{last_page} ({info['total']} total)")

        for d in chunk["data"]:
            cur = d.get("currency")
            dep, dst = d.get("departureCity") or {}, d.get("deliveryCity") or {}
            region = dep.get("region")
            region_slug = REGION_SLUGS.get(region)
            vehicle = d.get("vehicle") or {}
            deals.append({
                "id": d.get("id"),
                "reference": d.get("reference"),
                "type": d.get("type"),
                "status": d.get("status"),
                "route": d.get("name"),
                "region": region,
                "region_slug": region_slug,
                "vehicles_available": d.get("count"),
                "pickup": slim_city(dep),
                "dropoff": slim_city(dst),
                "available_from": d.get("available_from_date"),
                "available_to": d.get("available_to_date"),
                "earliest_departure": d.get("earliest_departure_date"),
                "latest_departure": d.get("latest_departure_date"),
                "hire_unit_type": d.get("hire_unit_type"),
                "rate_per_unit": money(d.get("hire_unit_rate"), cur),
                "included_units": d.get("hire_units_allowed"),
                "minimum_hire_units": d.get("minimum_hire_units"),
                "extra_units_allowed": d.get("extra_hire_units_allowed"),
                "extra_unit_rate": money(d.get("extra_hire_unit_rate"), cur),
                "retail_rate": money(d.get("retail_rate"), cur),
                "booking_fee": money(d.get("booking_fee_amount"), cur),
                "distance_allowed": d.get("distance_allowed"),
                "measurement": d.get("measurement"),
                "is_ferry_required": d.get("is_ferry_required"),
                "vehicle": {
                    "type": vehicle.get("type"),
                    "name": vehicle.get("name"),
                    "brand": vehicle.get("brand"),
                    "model": vehicle.get("model"),
                    "code": vehicle.get("code"),
                    "seatbelts": vehicle.get("seatbelts"),
                    "sleeps": vehicle.get("sleeps"),
                    "fuel": vehicle.get("fuel"),
                    "transmission": vehicle.get("transmission"),
                    "self_contained": vehicle.get("is_self_contained"),
                    "has_kitchen": vehicle.get("has_kitchen"),
                    "image": (d.get("images") or [{}])[0].get("url")
                             or vehicle.get("fallback_image_url"),
                },
                "supplier": (d.get("supplier") or {}).get("name"),
                "inclusions": [
                    {
                        "type": i.get("type"),
                        "value": i.get("value"),
                        "description": i.get("description"),
                        "has_free_tank": i.get("has_free_tank"),
                    }
                    for i in (d.get("inclusions") or [])
                ],
                "deal_link": f"{SITE}/relocations/deal/{d.get('deal_slug')}",
                "region_link": f"{SITE}/relocations/{region_slug}"
                               if region_slug else f"{SITE}/relocations",
            })
        page += 1
        time.sleep(args.delay)

    result = {
        "source": f"{SITE}/relocations"
                  + (f"/{REGION_SLUGS[regions[0]]}" if regions else ""),
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status_filter": statuses,
        "deal_count": len(deals),
        "deals": deals,
    }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    by_region = {}
    for d in deals:
        by_region[d["region_slug"] or "?"] = by_region.get(d["region_slug"] or "?", 0) + 1
    print(f"\nWrote {len(deals)} deals to {args.output}")
    print("By region: " + ", ".join(f"{k}={v}" for k, v in sorted(by_region.items())))


if __name__ == "__main__":
    main()
