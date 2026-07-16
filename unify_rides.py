#!/usr/bin/env python3
"""Unify roadsurfer / Movacar / Imoova scraper outputs into one JSON schema.

Reads the three per-site JSON files produced by:
  roadsurfer_rally_scraper.py -> roadsurfer_rally.json
  movacar_scraper.py          -> movacar_offers.json
  imoova_scraper.py           -> imoova_relocations.json

and writes a single file where every ride has the same shape:

  {
    "source":       "roadsurfer" | "movacar" | "imoova",
    "source_id":    original identifier,
    "deal_type":    "rally" | "relocation" | "gap_rental",
    "trip_type":    "one-way" | "round-trip",
    "route":        "Pickup -> Dropoff",
    "pickup":       { name, address, city, state, country, postal_code,
                      latitude, longitude, extra{...} },
    "dropoff":      { same shape },
    "vehicle":      { brand, model, name, category, type, seats, sleeps,
                      doors, fuel, transmission, image, extra{...} },
    "price":        { amount, currency, unit } | null,
    "availability": { windows: [{start, end}], earliest_pickup,
                      latest_pickup, latest_delivery, valid_until,
                      currently_bookable },
    "duration":     { included, included_unit, minimum, extra_allowed,
                      extra_rate{amount,currency} },
    "mileage":      { included_km, extra_cost_per_km{amount,currency},
                      distance_km },
    "supplier":     rental company / brand name,
    "links":        { booking, listing },
    "extra":        anything source-specific that has no unified slot,
    "raw":          the untouched original record
  }

Nothing is dropped: fields that don't fit the common schema land in the
`extra` objects, and the full original record is kept under `raw`.

Usage:
    uv run unify_rides.py [-o unified_rides.json]
        [--roadsurfer roadsurfer_rally.json] [--movacar movacar_offers.json]
        [--imoova imoova_relocations.json] [--no-raw]
"""

import argparse
import json
import sys
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        print(f"warning: {path} not found - skipping", file=sys.stderr)
        return None


def place(name=None, address=None, city=None, state=None, country=None,
          postal_code=None, latitude=None, longitude=None, **extra):
    return {
        "name": name,
        "address": address,
        "city": city,
        "state": state,
        "country": country,
        "postal_code": postal_code,
        "latitude": latitude,
        "longitude": longitude,
        "extra": {k: v for k, v in extra.items() if v not in (None, [], {})},
    }


def vehicle(brand=None, model=None, name=None, category=None, vtype=None,
            seats=None, sleeps=None, doors=None, fuel=None, transmission=None,
            image=None, **extra):
    return {
        "brand": brand,
        "model": model,
        "name": name,
        "category": category,
        "type": vtype,
        "seats": seats,
        "sleeps": sleeps,
        "doors": doors,
        "fuel": fuel,
        "transmission": transmission,
        "image": image,
        "extra": {k: v for k, v in extra.items() if v not in (None, [], {})},
    }


# --------------------------------------------------------------- roadsurfer

def map_roadsurfer(data):
    rides = []
    for r in data.get("rides", []):
        p, d = r.get("pickup") or {}, r.get("dropoff") or {}
        windows = [
            {"start": t.get("start_date"), "end": t.get("end_date")}
            for t in r.get("available_timeframes", [])
        ]
        rides.append({
            "source": "roadsurfer",
            "source_id": f"{p.get('id')}-{d.get('id')}",
            "deal_type": "rally",
            "trip_type": r.get("trip_type"),
            "route": r.get("route"),
            "pickup": place(
                name=p.get("name"), address=p.get("address"),
                city=p.get("city"), country=p.get("country"),
                postal_code=p.get("zip"),
                latitude=p.get("latitude"), longitude=p.get("longitude"),
                station_id=p.get("id"), country_code=p.get("country_code"),
                timezone=p.get("timezone"), google_maps=p.get("google_maps"),
                search_tags=p.get("search_tags"),
            ),
            "dropoff": place(
                name=d.get("name"), address=d.get("address"),
                city=d.get("city"), country=d.get("country"),
                postal_code=d.get("zip"),
                latitude=d.get("latitude"), longitude=d.get("longitude"),
                station_id=d.get("id"), country_code=d.get("country_code"),
                timezone=d.get("timezone"), google_maps=d.get("google_maps"),
                search_tags=d.get("search_tags"),
            ),
            "vehicle": vehicle(category="Campervan"),
            "price": None,  # roadsurfer's rally API does not expose pricing
            "availability": {
                "windows": windows,
                "earliest_pickup": windows[0]["start"] if windows else None,
                "latest_pickup": None,
                "latest_delivery": windows[-1]["end"] if windows else None,
                "valid_until": None,
                "currently_bookable": bool(r.get("available")),
            },
            "duration": {
                "included": None, "included_unit": "fixed_window",
                "minimum": None, "extra_allowed": None, "extra_rate": None,
            },
            "mileage": {
                "included_km": None, "extra_cost_per_km": None,
                "distance_km": None,
            },
            "supplier": "roadsurfer",
            "links": {
                "booking": r.get("booking_link"),
                "listing": data.get("source"),
            },
            "extra": {},
            "raw": r,
        })
    return rides


# ------------------------------------------------------------------ movacar

def _movacar_supplier(brand_image):
    """Derive a supplier name from the brand logo filename
    (e.g. 'SixtLogo.png' -> 'Sixt', 'IndieCampers.svg' -> 'IndieCampers')."""
    if not brand_image:
        return None
    name = brand_image.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if name.endswith("Logo"):
        name = name[:-4]
    return name or None


def map_movacar(data):
    rides = []
    for r in data.get("offers", []):
        p, d = r.get("pickup") or {}, r.get("dropoff") or {}
        v = r.get("vehicle") or {}
        price = r.get("price") or {}
        hours = r.get("included_period_hours")
        rides.append({
            "source": "movacar",
            "source_id": r.get("id"),
            "deal_type": "relocation",
            "trip_type": "one-way",
            "route": r.get("route"),
            "pickup": place(
                name=p.get("city"), address=p.get("street"),
                city=p.get("city"), postal_code=p.get("postal_code"),
                latitude=p.get("latitude"), longitude=p.get("longitude"),
                station_id=p.get("station_id"), station_code=p.get("code"),
            ),
            "dropoff": place(
                name=d.get("city"), address=d.get("street"),
                city=d.get("city"), postal_code=d.get("postal_code"),
                latitude=d.get("latitude"), longitude=d.get("longitude"),
                station_id=d.get("station_id"), station_code=d.get("code"),
            ),
            "vehicle": vehicle(
                brand=v.get("make"), model=v.get("model"),
                name=v.get("description"), category=v.get("category"),
                vtype=v.get("type"), seats=v.get("seats"),
                doors=v.get("doors"), fuel=v.get("fuel_type"),
                transmission=v.get("gear_type"), image=v.get("image"),
                vehicle_class=v.get("class"),
            ),
            "price": {
                "amount": price.get("amount"),
                "currency": price.get("currency"),
                "unit": "total",
            } if price else None,
            "availability": {
                "windows": [{
                    "start": r.get("earliest_pickup"),
                    "end": r.get("latest_delivery"),
                }],
                "earliest_pickup": r.get("earliest_pickup"),
                "latest_pickup": r.get("latest_pickup"),
                "latest_delivery": r.get("latest_delivery"),
                "valid_until": r.get("valid_until"),
                "currently_bookable": True,
            },
            "duration": {
                "included": hours / 24.0 if hours else None,
                "included_unit": "days",
                "minimum": None,
                "extra_allowed": (r.get("extra_period_hours") or 0) / 24.0
                                 or None,
                "extra_rate": None,
            },
            "mileage": {
                "included_km": r.get("free_km"),
                "extra_cost_per_km": {
                    "amount": r["cents_per_extra_km"] / 100.0,
                    "currency": (price or {}).get("currency") or "EUR",
                } if r.get("cents_per_extra_km") is not None else None,
                "distance_km": r.get("route_distance_km"),
            },
            "supplier": _movacar_supplier(r.get("brand_image")),
            "links": {
                "booking": r.get("booking_link"),
                "listing": r.get("search_link"),
            },
            "extra": {
                "offer_type": r.get("offer_type"),
                "min_drivers_age": r.get("min_drivers_age"),
                "has_connections": r.get("has_connections"),
                "brand_image": r.get("brand_image"),
            },
            "raw": r,
        })
    return rides


# ------------------------------------------------------------------- imoova

def map_imoova(data):
    rides = []
    for r in data.get("deals", []):
        p, d = r.get("pickup") or {}, r.get("dropoff") or {}
        v = r.get("vehicle") or {}
        rate = r.get("rate_per_unit") or {}
        unit_map = {"twenty_four_hours": "day", "day": "day", "night": "night",
                    "week": "week", "hour": "hour"}
        raw_unit = (r.get("hire_unit_type") or "day").lower()
        unit = unit_map.get(raw_unit, raw_unit)
        same_city = p.get("id") is not None and p.get("id") == d.get("id")
        distance_allowed = r.get("distance_allowed")
        rides.append({
            "source": "imoova",
            "source_id": r.get("reference"),
            "deal_type": (r.get("type") or "").lower() or "relocation",
            "trip_type": "round-trip" if same_city else "one-way",
            "route": r.get("route"),
            "pickup": place(
                name=p.get("name"), city=p.get("name"), state=p.get("state"),
                latitude=p.get("latitude"), longitude=p.get("longitude"),
                city_id=p.get("id"), region=p.get("region"),
                region_slug=p.get("region_slug"),
            ),
            "dropoff": place(
                name=d.get("name"), city=d.get("name"), state=d.get("state"),
                latitude=d.get("latitude"), longitude=d.get("longitude"),
                city_id=d.get("id"), region=d.get("region"),
                region_slug=d.get("region_slug"),
            ),
            "vehicle": vehicle(
                brand=v.get("brand"), model=v.get("model"),
                name=v.get("name"), vtype=v.get("type"),
                seats=v.get("seatbelts"), sleeps=v.get("sleeps"),
                fuel=v.get("fuel"), transmission=v.get("transmission"),
                image=v.get("image"), code=v.get("code"),
                self_contained=v.get("self_contained"),
                has_kitchen=v.get("has_kitchen"),
            ),
            "price": {
                "amount": rate.get("amount"),
                "currency": rate.get("currency"),
                "unit": f"per_{unit}",
            } if rate else None,
            "availability": {
                "windows": [{
                    "start": r.get("available_from"),
                    "end": r.get("available_to"),
                }],
                "earliest_pickup": r.get("earliest_departure"),
                "latest_pickup": r.get("latest_departure"),
                "latest_delivery": r.get("available_to"),
                "valid_until": None,
                "currently_bookable": r.get("status") == "READY",
            },
            "duration": {
                "included": r.get("included_units"),
                "included_unit": unit + "s" if not unit.endswith("s") else unit,
                "minimum": r.get("minimum_hire_units"),
                "extra_allowed": r.get("extra_units_allowed"),
                "extra_rate": r.get("extra_unit_rate"),
            },
            "mileage": {
                "included_km": distance_allowed
                               if r.get("measurement") == "METRIC" else None,
                "extra_cost_per_km": None,
                "distance_km": None,
            },
            "supplier": r.get("supplier"),
            "links": {
                "booking": r.get("deal_link"),
                "listing": r.get("region_link"),
            },
            "extra": {
                "status": r.get("status"),
                "region": r.get("region"),
                "region_slug": r.get("region_slug"),
                "vehicles_available": r.get("vehicles_available"),
                "retail_rate": r.get("retail_rate"),
                "booking_fee": r.get("booking_fee"),
                "is_ferry_required": r.get("is_ferry_required"),
                "measurement": r.get("measurement"),
                "distance_allowed_raw": distance_allowed,
                "inclusions": r.get("inclusions"),
            },
            "raw": r,
        })
    return rides


def main():
    ap = argparse.ArgumentParser(description="Unify the three ride feeds")
    ap.add_argument("-o", "--output", default="unified_rides.json")
    ap.add_argument("--roadsurfer", default="roadsurfer_rally.json")
    ap.add_argument("--movacar", default="movacar_offers.json")
    ap.add_argument("--imoova", default="imoova_relocations.json")
    ap.add_argument("--no-raw", action="store_true",
                    help="omit the raw per-source records to shrink the file")
    args = ap.parse_args()

    sources_meta = {}
    rides = []
    for name, path, mapper in [
        ("roadsurfer", args.roadsurfer, map_roadsurfer),
        ("movacar", args.movacar, map_movacar),
        ("imoova", args.imoova, map_imoova),
    ]:
        data = load(path)
        if data is None:
            continue
        mapped = mapper(data)
        rides.extend(mapped)
        sources_meta[name] = {
            "file": path,
            "scraped_at": data.get("scraped_at"),
            "listing_url": data.get("source"),
            "ride_count": len(mapped),
        }
        print(f"{name}: {len(mapped)} rides")

    if args.no_raw:
        for r in rides:
            r.pop("raw", None)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources_meta,
        "ride_count": len(rides),
        "rides": rides,
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"\nWrote {len(rides)} unified rides to {args.output}")


if __name__ == "__main__":
    main()
