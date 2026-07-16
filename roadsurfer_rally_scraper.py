#!/usr/bin/env python3
"""Scraper for roadsurfer Rally (https://booking.roadsurfer.com/en/rally/).

The rally booking funnel is a Nuxt SPA backed by a small JSON API. Each request
must carry an ``X-Requested-Alias`` header matching the endpoint, otherwise the
server answers 404 with the SPA's HTML shell. Endpoints used:

  GET /api/en/rally/stations                     (alias: rally.startStations)
      -> all pickup stations with address, city, coordinates, opening hours...
  GET /api/en/rally/stations/{id}                (alias: rally.fetchRoutes)
      -> single station detail incl. ``returns``: allowed dropoff station ids
  GET /api/en/rally/timeframes/{start}-{end}     (alias: rally.timeframes)
      -> list of bookable date windows for that pickup->dropoff route

Output: a JSON file with every pickup station, its possible dropoff(s), the
currently offered date windows and a booking deep link.

Usage:
    python roadsurfer_rally_scraper.py [-o roadsurfer_rally.json]
        [--lang en] [--only-available] [--delay 0.1]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

# Windows consoles may use a legacy codepage that can't print station names
# like "Öhningen"; force UTF-8 so progress output never crashes the scrape.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://booking.roadsurfer.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class RallyClient:
    def __init__(self, lang="en", delay=0.1, timeout=30, retries=3):
        self.lang = lang
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, path, alias):
        url = f"{BASE}/api/{self.lang}{path}"
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(
                    url,
                    headers={"X-Requested-Alias": alias},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                time.sleep(self.delay)
                return data
            except (requests.RequestException, ValueError) as err:
                last_err = err
                if attempt < self.retries:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"GET {url} failed after {self.retries} tries: {last_err}")

    def start_stations(self):
        return self._get("/rally/stations", "rally.startStations")

    def station_detail(self, station_id):
        return self._get(f"/rally/stations/{station_id}", "rally.fetchRoutes")

    def timeframes(self, start_id, end_id):
        return self._get(
            f"/rally/timeframes/{start_id}-{end_id}", "rally.timeframes"
        )


def slim_station(st):
    """Keep the useful station fields for the output."""
    city = st.get("city") or {}
    coord = st.get("coordinate") or {}
    return {
        "id": st.get("id"),
        "name": st.get("name"),
        "address": st.get("address"),
        "zip": st.get("zip"),
        "city": city.get("name"),
        "country_code": city.get("country"),
        "country": city.get("country_name"),
        "latitude": coord.get("latitude"),
        "longitude": coord.get("longitude"),
        "timezone": st.get("timezone"),
        "google_maps": st.get("google_link"),
        "search_tags": st.get("search_tags") or [],
    }


def main():
    ap = argparse.ArgumentParser(description="Scrape roadsurfer rally offers")
    ap.add_argument("-o", "--output", default="roadsurfer_rally.json")
    ap.add_argument("--lang", default="en", help="API language segment (default: en)")
    ap.add_argument("--delay", type=float, default=0.1, help="pause between requests")
    ap.add_argument(
        "--only-available",
        action="store_true",
        help="only include routes that currently have bookable timeframes",
    )
    args = ap.parse_args()

    client = RallyClient(lang=args.lang, delay=args.delay)

    print("Fetching start stations...")
    stations = client.start_stations()
    print(f"  {len(stations)} pickup stations")
    station_map = {st["id"]: st for st in stations}

    rides = []
    for i, st in enumerate(stations, 1):
        start_id = st["id"]
        label = f"[{i}/{len(stations)}] {st['name']} (#{start_id})"

        # The list payload often has empty `returns`; the detail endpoint is
        # authoritative for the allowed dropoff stations.
        try:
            detail = client.station_detail(start_id)
        except RuntimeError as err:
            print(f"{label}: detail failed - {err}", file=sys.stderr)
            detail = st
        dropoff_ids = detail.get("returns") or []
        one_way = bool(detail.get("one_way"))
        if not dropoff_ids:
            dropoff_ids = [start_id]  # round trip: return to the same station

        for end_id in dropoff_ids:
            if end_id not in station_map:
                try:
                    station_map[end_id] = client.station_detail(end_id)
                except RuntimeError as err:
                    print(f"{label}: dropoff #{end_id} lookup failed - {err}",
                          file=sys.stderr)
                    station_map[end_id] = {"id": end_id, "name": f"station {end_id}"}

            try:
                frames = client.timeframes(start_id, end_id)
            except RuntimeError as err:
                print(f"{label}: timeframes {start_id}-{end_id} failed - {err}",
                      file=sys.stderr)
                frames = []

            timeframes = sorted(
                (
                    {
                        "start_date": f.get("startDate"),
                        "end_date": f.get("endDate"),
                    }
                    for f in frames
                ),
                key=lambda x: (x["start_date"] or "", x["end_date"] or ""),
            )
            if args.only_available and not timeframes:
                continue

            rides.append(
                {
                    "route": f"{st['name']} -> {station_map[end_id].get('name')}",
                    "trip_type": "one-way" if (one_way and end_id != start_id)
                                 else "round-trip",
                    "pickup": slim_station(detail),
                    "dropoff": slim_station(station_map[end_id]),
                    "available": bool(timeframes),
                    "available_timeframes": timeframes,
                    "booking_link":
                        f"{BASE}/{args.lang}/rally/?station={start_id}",
                }
            )
        print(f"{label}: {len(dropoff_ids)} route(s)")

    result = {
        "source": f"{BASE}/{args.lang}/rally/",
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "station_count": len(stations),
        "ride_count": len(rides),
        "rides": rides,
    }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    available = sum(1 for r in rides if r["available"])
    print(f"\nWrote {len(rides)} rides ({available} currently bookable) "
          f"to {args.output}")


if __name__ == "__main__":
    main()
