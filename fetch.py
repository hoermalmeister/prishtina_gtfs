"""
Stáhne aktuální data Trafiku Urban (Priština) do data/raw.json.gz:
  /stations, /zones a jízdní řády pro každou dvojici směr–zastávka (/schedules).
Spouští se denně přes GitHub Actions. Pokud se nepodaří stáhnout zastávky,
linky nebo víc než MAX_FAILED_SHARE jízdních řádů, stará data se nepřepíšou.
"""

import gzip
import json
import os
import sys
import time

import requests

API = "https://tu.appbites.dev/api/public"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
           "User-Agent": "prishtina_gtfs (+https://github.com/hoermalmeister/prishtina_gtfs)"}
OUT = os.path.join("data", "raw.json.gz")
DELAY_S = 0.15
MAX_FAILED_SHARE = 0.05


def get(session, path):
    for attempt in range(3):
        try:
            r = session.get(API + path, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return r.json() if r.headers.get("content-type", "").startswith("application/json") else None
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"  CHYBA {path}: {e}")
                return None
            time.sleep(2 * (attempt + 1))


def main():
    s = requests.Session()
    stations, zones = get(s, "/stations"), get(s, "/zones")
    if not stations or not stations.get("data") or not zones or not zones.get("data"):
        sys.exit("Nepodařilo se stáhnout zastávky nebo linky – data neukládám.")
    print(f"Zastávek {len(stations['data'])}, linek {len(zones['data'])}")

    pairs = []
    for line in zones["data"]:
        for key in ("on_line_going", "on_line_return"):
            d = line.get(key)
            if d:
                pairs += [(d["_id"], st["_id"]) for st in d["ordered_stations"]]
    pairs = list(dict.fromkeys(pairs))

    schedules, failed = {}, 0
    for n, (direction, station) in enumerate(pairs, 1):
        res = get(s, f"/schedules/{direction}/{station}")
        if res is None:
            failed += 1
            schedules[f"{direction}/{station}"] = None
        else:
            # {"success":true,"data":{...}} nebo {"success":false,"data":"Schedule not found"}
            schedules[f"{direction}/{station}"] = res.get("data")
        if n % 200 == 0:
            print(f"  {n}/{len(pairs)}")
        time.sleep(DELAY_S)

    if failed > MAX_FAILED_SHARE * len(pairs):
        sys.exit(f"Selhalo {failed} z {len(pairs)} jízdních řádů – data neukládám.")

    os.makedirs("data", exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "stations": stations, "zones": zones, "schedules": schedules},
                  fh, ensure_ascii=False, separators=(",", ":"))
    found = sum(1 for v in schedules.values() if isinstance(v, dict))
    print(f"Uloženo {OUT}: {len(pairs)} dvojic, s jízdním řádem {found}, selhalo {failed}")


if __name__ == "__main__":
    main()
