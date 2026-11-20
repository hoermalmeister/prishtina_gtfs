"""
GTFS-Realtime server pro MHD Priština (Trafiku Urban) – nasazení na Railway.

  * drží spojení se socket.io serverem aplikace (wss://tu.appbites.dev, událost
    bus_update – kompletní seznam vozidel zhruba každé 3 s),
  * každých BUILD_INTERVAL s z poslední zprávy postaví VehiclePositions a TripUpdates,
  * statický GTFS (kvůli párování) stahuje z GitHubu a obnovuje.

Endpointy:
  /vehicle_positions.pb, /trip_updates.pb   – GTFS-RT
  /vehicle_positions.json, /trip_updates.json – ladění
  /status        – statistiky posledního cyklu
  /debug/vehicles.json – poslední surová zpráva bus_update
  /healthz       – proces běží

Proměnné prostředí (vše volitelné):
  STATIC_GTFS_URL       odkud brát statický GTFS
  STATIC_REFRESH_HOURS  jak často ho obnovit (12)
  BUILD_INTERVAL        s mezi stavbou feedů (10)
  INCLUDE_INACTIVE      1 = brát i vozidla s is_active=false (výchozí 0)
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests
import socketio
from flask import Flask, Response, jsonify
from google.protobuf.json_format import MessageToDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prishtina_rt as rt  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
STATIC_GTFS_URL = os.environ.get(
    "STATIC_GTFS_URL", "https://github.com/hoermalmeister/prishtina_gtfs/raw/main/prishtina_gtfs.zip")
STATIC_REFRESH_HOURS = float(os.environ.get("STATIC_REFRESH_HOURS", "12"))
BUILD_INTERVAL = int(os.environ.get("BUILD_INTERVAL", "10"))
INCLUDE_INACTIVE = os.environ.get("INCLUDE_INACTIVE", "0") == "1"
SOCKET_URL = "https://tu.appbites.dev"
SOCKET_PATH = "/ws/socket.io"

app = Flask(__name__)
_lock = threading.Lock()
_state = {
    "static": None, "static_at": 0.0,
    "vehicles": [], "vehicles_at": 0.0, "socket_connected": False,
    "vp_pb": rt.new_feed(time.time()).SerializeToString(),
    "tu_pb": rt.new_feed(time.time()).SerializeToString(),
    "vp_dict": {}, "tu_dict": {}, "built_at": 0.0, "stats": {}, "error": None,
}
_previous = {}


# --- socket.io ------------------------------------------------------------------
sio = socketio.Client(reconnection=True, reconnection_delay=2, reconnection_delay_max=30)


@sio.event
def connect():
    with _lock:
        _state["socket_connected"] = True
    print("socket připojen")


@sio.event
def disconnect():
    with _lock:
        _state["socket_connected"] = False
    print("socket odpojen")


@sio.on("bus_update")
def on_bus_update(data):
    if isinstance(data, list):
        with _lock:
            _state["vehicles"] = data
            _state["vehicles_at"] = time.time()


def socket_loop():
    while True:
        try:
            sio.connect(SOCKET_URL, socketio_path=SOCKET_PATH, transports=["websocket"])
            sio.wait()
        except Exception as e:  # noqa: BLE001
            print(f"socket chyba: {type(e).__name__}: {e}")
        time.sleep(5)


# --- statický GTFS a stavba feedů --------------------------------------------------
def ensure_static():
    if _state["static"] and time.time() - _state["static_at"] < STATIC_REFRESH_HOURS * 3600:
        return
    r = requests.get(STATIC_GTFS_URL, timeout=120)
    r.raise_for_status()
    static = rt.load_static(r.content)
    with _lock:
        _state["static"], _state["static_at"] = static, time.time()
    print(f"statický GTFS načten: {sum(len(v) for v in static['by_dir'].values())} spojů")


def build_once():
    ensure_static()
    with _lock:
        vehicles, received = list(_state["vehicles"]), _state["vehicles_at"]
    if not vehicles:
        raise RuntimeError("zatím žádná zpráva bus_update")
    if time.time() - received > 120:
        raise RuntimeError(f"poslední bus_update je {int(time.time() - received)} s starý")
    vp, tu, stats = rt.build_feeds(vehicles, _state["static"], received, INCLUDE_INACTIVE, _previous)
    with _lock:
        _state.update(vp_pb=vp.SerializeToString(), tu_pb=tu.SerializeToString(),
                      vp_dict=MessageToDict(vp), tu_dict=MessageToDict(tu),
                      built_at=time.time(), stats=stats, error=None)
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {stats} | VP={len(vp.entity)} TU={len(tu.entity)}")


def build_loop():
    while True:
        try:
            build_once()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state["error"] = f"{type(e).__name__}: {e}"
            print(f"CHYBA: {type(e).__name__}: {e}")
        time.sleep(BUILD_INTERVAL)


# --- HTTP ----------------------------------------------------------------------------
def _pb(key):
    with _lock:
        return Response(_state[key], mimetype="application/x-protobuf")


@app.route("/vehicle_positions.pb")
def vp_pb():
    return _pb("vp_pb")


@app.route("/trip_updates.pb")
def tu_pb():
    return _pb("tu_pb")


@app.route("/vehicle_positions.json")
def vp_json():
    with _lock:
        return jsonify(_state["vp_dict"])


@app.route("/trip_updates.json")
def tu_json():
    with _lock:
        return jsonify(_state["tu_dict"])


@app.route("/debug/vehicles.json")
def debug_vehicles():
    with _lock:
        return jsonify({"received_at": _state["vehicles_at"], "vehicles": _state["vehicles"]})


@app.route("/healthz")
def healthz():
    return jsonify({"alive": True})


@app.route("/status")
def status():
    with _lock:
        age = time.time() - _state["built_at"] if _state["built_at"] else None
        body = {"ok": age is not None and age < BUILD_INTERVAL * 3 and _state["error"] is None,
                "age_s": age, "error": _state["error"], "socket_connected": _state["socket_connected"],
                "last_bus_update_age_s": time.time() - _state["vehicles_at"] if _state["vehicles_at"] else None,
                "stats": _state["stats"]}
    return jsonify(body), (200 if body["ok"] else 503)


@app.route("/")
def index():
    with _lock:
        s, err = _state["stats"], _state["error"]
    html = f"""<!doctype html><html lang="cs"><meta charset="utf-8"><title>Prishtina GTFS-RT</title>
<body style="font-family:system-ui;max-width:640px;margin:40px auto;line-height:1.5">
<h1>Trafiku Urban Prishtinë – GTFS-Realtime</h1><p>Poslední cyklus: {s}</p><p>Chyba: {err or "žádná"}</p>
<ul><li><a href="/vehicle_positions.pb">/vehicle_positions.pb</a></li><li><a href="/trip_updates.pb">/trip_updates.pb</a></li>
<li><a href="/vehicle_positions.json">/vehicle_positions.json</a></li><li><a href="/trip_updates.json">/trip_updates.json</a></li>
<li><a href="/status">/status</a></li><li><a href="/debug/vehicles.json">/debug/vehicles.json</a></li></ul></body></html>"""
    return Response(html, mimetype="text/html")


threading.Thread(target=socket_loop, daemon=True).start()
threading.Thread(target=build_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
