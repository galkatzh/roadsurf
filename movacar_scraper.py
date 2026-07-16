#!/usr/bin/env python3
"""Scraper for Movacar one-way relocation offers (https://www.movacar.com).

The site is an Angular SPA backed by an open JSON API:

  GET {API}/v1/offers?locale=en&pickupDateFrom=YYYY-MM-DD&pickupDateTo=YYYY-MM-DD
      -> all bookable offers in the window (JSON:API style: data[] +
         included[] stations and monetary amounts)
  GET {API}/v1/locations/offers?locale=en
      -> city-level origin/destination summaries with coordinates

An offer's `id` is "{offerId}_{originStationId}_{destinationStationId}"; the
booking page is  https://www.movacar.com/en-US/checkout/{offerId}?origin={o}&destination={d}

Output: a JSON file with every offer incl. vehicle, pickup/dropoff station,
dates, price, mileage allowance and booking links.

Usage:
    uv run --with requests movacar_scraper.py [-o movacar_offers.json]
        [--days 365] [--locale en]
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone, timedelta

import requests

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

API = "https://crowd-api-production-615013621295.europe-west1.run.app"
SITE = "https://www.movacar.com"

# Official label mappings from the site's translation file (/assets/i18n/en.json)
FUEL_TYPES = {
    "crowd_vehicle_fuel_type_0": "Petrol",
    "crowd_vehicle_fuel_type_1": "Diesel",
    "crowd_vehicle_fuel_type_2": "Hybrid",
    "crowd_vehicle_fuel_type_3": "Electric",
    "crowd_vehicle_fuel_type_4": "Hydrogen",
    "crowd_vehicle_fuel_type_5": "Autogas (LPG)",
    "crowd_vehicle_fuel_type_6": "Multi-Fuel",
    "crowd_vehicle_fuel_type_7": "Ethanol",
    "crowd_vehicle_fuel_type_10": "Unspecified",
}
GEAR_TYPES = {
    "crowd_vehicle_gear_type_0": "Manual",
    "crowd_vehicle_gear_type_1": "Automatic",
    "crowd_vehicle_gear_type_2": "Can be Manual/Automatic",
}
VEHICLE_TYPES = {
    "crowd_vehicle_type_0": "Car",
    "crowd_vehicle_type_1": "Electric Car",
    "crowd_vehicle_type_2": "Transporter / 9-Seater",
    "crowd_vehicle_type_5": "Camper",
}


def label(mapping, key):
    """Translate a crowd_* key; fall back to the raw key for unknown values."""
    return mapping.get(key, key) if key else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": SITE,
    "Referer": SITE + "/",
}


def get(session, path, params):
    resp = session.get(API + path, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def slim_station(st):
    if not st:
        return None
    a = st.get("attributes", {})
    return {
        "station_id": st.get("id"),
        "city": a.get("city"),
        "street": a.get("street"),
        "postal_code": a.get("postal_code"),
        "latitude": a.get("latitude"),
        "longitude": a.get("longitude"),
        "code": a.get("code"),
    }


def money(m):
    if not m:
        return None
    a = m.get("attributes", {})
    minor = a.get("amount_minor_units")
    return {
        "amount": None if minor is None else minor / 100.0,
        "currency": a.get("currency"),
    }


def ts_to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser(description="Scrape Movacar relocation offers")
    ap.add_argument("-o", "--output", default="movacar_offers.json")
    ap.add_argument("--days", type=int, default=365,
                    help="pickup window length in days from today (default 365)")
    ap.add_argument("--locale", default="en")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    date_from = date.today()
    date_to = date_from + timedelta(days=args.days)
    print(f"Fetching offers {date_from} .. {date_to} ...")

    payload = get(session, "/v1/offers", {
        "locale": args.locale,
        "pickupDateFrom": date_from.isoformat(),
        "pickupDateTo": date_to.isoformat(),
    })

    included = {(i["type"], i["id"]): i for i in payload.get("included", [])}

    def resolve(rel):
        d = (rel or {}).get("data")
        return included.get((d["type"], d["id"])) if d else None

    offers = []
    for item in payload.get("data", []):
        a = item.get("attributes", {})
        rel = item.get("relationships", {})
        origin = resolve(rel.get("origin"))
        destination = resolve(rel.get("destination"))
        price = money(resolve(rel.get("base_price")))

        offer_id = str(a.get("offer_id") or item.get("id", "").split("_")[0])
        origin_id = origin["id"] if origin else None
        dest_id = destination["id"] if destination else None
        checkout = (f"{SITE}/en-US/checkout/{offer_id}"
                    f"?origin={origin_id}&destination={dest_id}")

        origin_city = origin["attributes"].get("city") if origin else None
        dest_city = destination["attributes"].get("city") if destination else None

        offers.append({
            "id": item.get("id"),
            "offer_id": offer_id,
            "route": f"{origin_city} -> {dest_city}",
            "offer_type": a.get("offer_type"),
            "vehicle": {
                "make": a.get("v_make") or a.get("make"),
                "model": a.get("v_model") or a.get("model"),
                "description": a.get("model_description"),
                "category": a.get("vehicle_category_name"),
                "type": label(VEHICLE_TYPES, a.get("vehicle_type")),
                "class": a.get("movacar_class"),
                "seats": a.get("seats"),
                "doors": a.get("doors"),
                "fuel_type": label(FUEL_TYPES, a.get("fuel_type")),
                "gear_type": label(GEAR_TYPES, a.get("gear_type")),
                "image": a.get("vehicle_image_url"),
            },
            "brand_image": a.get("brand_image_url"),
            "pickup": slim_station(origin),
            "dropoff": slim_station(destination),
            "earliest_pickup": a.get("start_date"),
            "latest_delivery": a.get("end_date"),
            "latest_pickup": ts_to_iso(a.get("latest_pickup")),
            "valid_until": a.get("valid_until"),
            "included_period_hours": a.get("period"),
            "extra_period_hours": a.get("extra_period"),
            "price": price,
            "free_km": a.get("free_km"),
            "cents_per_extra_km": a.get("cents_per_extra_km"),
            "route_distance_km": round(a["distance"] / 1000.0, 1)
                                 if a.get("distance") else None,
            "min_drivers_age": a.get("min_drivers_age"),
            "has_connections": a.get("has_connections"),
            "booking_link": checkout,
            "search_link": (f"{SITE}/en-US/offers"
                            f"?pickupDateFrom={date_from}&pickupDateTo={date_to}"),
        })

    result = {
        "source": f"{SITE}/en-US/offers",
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pickup_window": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "offer_count": len(offers),
        "offers": offers,
    }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"Wrote {len(offers)} offers to {args.output}")


if __name__ == "__main__":
    main()
