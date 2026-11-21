"""
Sbírá odpovědi POST /buses/{busId}/upcomingStations (to, co aplikace ukáže po
kliknutí na vozidlo) pro aktivní vozidla linek, které mají v API jízdní řád jen
na výchozí zastávce. Z nich se pak spočítají jízdní doby mezi zastávkami.

Spuštění (Windows):
  pip install requests "python-socketio[client]"
  python prishtina_collect_upcoming.py              # 90 minut, linky bez časů po trase
  python prishtina_collect_upcoming.py 60 all       # 60 minut, všechny linky (kalibrace)

Ukládá průběžně do upcoming_samples.jsonl (každý řádek = jedna odpověď), takže
je použitelný i při předčasném ukončení (Ctrl+C). Ideálně pusť přes den,
kdy jezdí hodně spojů, klidně 1–2 hodiny.
"""

import json
import sys
import threading
import time

import requests
import socketio

API = "https://tu.appbites.dev/api/public"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
TARGET_LINES = {"6", "6A", "7", "7A", "7B", "8", "10", "10A", "10B", "10C", "11", "12",
                "13", "16", "17", "20", "22", "23", "25", "3A"}
INTERVAL_S = 60            # jak často se ptát na každé vozidlo
DELAY_BETWEEN_S = 0.3      # pauza mezi dotazy
OUT = "upcoming_samples.jsonl"
GEOM_OUT = "line_geometry.json"

minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 90
ALL_LINES = len(sys.argv) > 2 and sys.argv[2].lower() == "all"
geometries = {}
latest = {"vehicles": [], "t": 0}
lock = threading.Lock()

sio = socketio.Client(reconnection=True)


@sio.on("bus_update")
def on_update(data):
    if isinstance(data, list):
        with lock:
            latest["vehicles"], latest["t"] = data, time.time()


def main():
    sio.connect("https://tu.appbites.dev", socketio_path="/ws/socket.io", transports=["websocket"])
    print("Čekám na první polohy...")
    while not latest["vehicles"]:
        time.sleep(1)
    end = time.time() + minutes * 60
    n = 0
    with open(OUT, "a", encoding="utf-8") as fh:
        while time.time() < end:
            cycle_start = time.time()
            with lock:
                vehicles = list(latest["vehicles"])
            targets = [v for v in vehicles
                       if v.get("is_active") and (ALL_LINES or str(v.get("number", "")).strip() in TARGET_LINES)
                       and "NO RECENT DATA" not in (v.get("name") or "").upper()]
            for v in targets:
                try:
                    r = requests.post(f"{API}/buses/{v['_id']}/upcomingStations", headers=HEADERS,
                                      json={"location": [0, 0], "userId": "unknown"}, timeout=20)
                    body = r.json()
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict) and data.get("line_geometry") is not None:
                        lid = (v.get("line") or {}).get("_id")
                        if lid and lid not in geometries:
                            geometries[lid] = data["line_geometry"]
                            with open(GEOM_OUT, "w", encoding="utf-8") as gh:
                                json.dump(geometries, gh, ensure_ascii=False)
                        data.pop("line_geometry", None)      # geometrie se ukládá jen jednou
                except Exception as e:  # noqa: BLE001
                    body = {"error": str(e)}
                fh.write(json.dumps({"t": time.time(), "vehicle": {
                    "_id": v["_id"], "name": v.get("name"), "number": v.get("number"),
                    "line_id": (v.get("line") or {}).get("_id"),
                    "coordinates": (v.get("geometry") or {}).get("coordinates"),
                    "heading": v.get("heading")}, "response": body}, ensure_ascii=False) + "\n")
                fh.flush()
                n += 1
                time.sleep(DELAY_BETWEEN_S)
            left = int((end - time.time()) / 60)
            print(f"{time.strftime('%H:%M:%S')}  vozidel v cílových linkách: {len(targets)}, "
                  f"uloženo celkem {n}, zbývá ~{left} min")
            time.sleep(max(0, INTERVAL_S - (time.time() - cycle_start)))
    sio.disconnect()
    print(f"Hotovo, {n} odpovědí v {OUT}, geometrie {len(geometries)} směrů v {GEOM_OUT}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\nPřerušeno – data jsou v {OUT}")
