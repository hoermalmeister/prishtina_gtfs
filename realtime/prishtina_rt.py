"""
Převod poloh vozidel Trafiku Urban (socket.io, událost bus_update) na GTFS-Realtime.

Zprávy nesou polohu, azimut, linku a směr (line._id = ID směru ze /zones), ale
ne konkrétní spoj. Spoj se určí podle polohy:
  1. poloha se promítne na trasu každého spoje daného směru, který dnes jede
     (lomená čára přes zastávky s jízdním řádem),
  2. z jízdního řádu se interpoluje, kdy tam spoj měl být, a rozdíl proti
     aktuálnímu času je zpoždění,
  3. vybere se spoj s nejmenší odchylkou v okně MIN_DELAY..MAX_DELAY; každý spoj
     dostane nejvýš jedno vozidlo (přednost má menší odchylka).
"""

import csv
import io
import math
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

TZ = ZoneInfo("Europe/Belgrade")
MIN_DELAY_MIN = -10         # nejvíc o kolik minut smí vozidlo jet napřed
MAX_DELAY_MIN = 40          # nejvíc o kolik minut smí být zpožděné
MAX_OFF_ROUTE_M = 400       # max. vzdálenost od trasy spoje
AT_STOP_M = 40              # blíž než tohle = stojí na zastávce
STICKY_BONUS_MIN = 6        # zvýhodnění spoje, ke kterému bylo vozidlo přiřazeno minule
VP = gtfs_realtime_pb2.VehiclePosition


# --- statický feed ------------------------------------------------------------------
def load_static(zip_bytes_or_path):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes_or_path) if isinstance(zip_bytes_or_path, bytes)
                         else zip_bytes_or_path)

    def read(name):
        with zf.open(name) as fh:
            return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))

    stops = {s["stop_id"]: (float(s["stop_lat"]), float(s["stop_lon"])) for s in read("stops.txt")}
    calendar = {c["service_id"]: c for c in read("calendar.txt")}
    trips = {t["trip_id"]: t for t in read("trips.txt")}
    times = defaultdict(list)
    for st in read("stop_times.txt"):
        h, m, _s = map(int, st["departure_time"].split(":"))
        times[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"], h * 60 + m))
    by_dir = defaultdict(list)
    for tid, t in trips.items():
        seq = sorted(times[tid])
        if len(seq) < 2:
            continue
        direction = tid.rsplit("_", 2)[0]
        pts = [stops[s] for _q, s, _t in seq]
        by_dir[direction].append({"trip_id": tid, "route_id": t["route_id"], "service_id": t["service_id"],
                                  "stops": seq, "pts": pts})
    return {"by_dir": by_dir, "calendar": calendar, "stops": stops}


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def service_active(static, service_id, day):
    c = static["calendar"].get(service_id)
    if not c:
        return False
    key = day.strftime("%Y%m%d")
    return c["start_date"] <= key <= c["end_date"] and c.get(_WEEKDAYS[day.weekday()]) == "1"


# --- geometrie ----------------------------------------------------------------------------
def _xy(lat, lon, lat0):
    return (math.radians(lon) * math.cos(math.radians(lat0)) * 6371000.0,
            math.radians(lat) * 6371000.0)


def project(point, pts):
    """Promítne bod na lomenou čáru. Vrátí (index úseku, podíl 0..1, vzdálenost v m)."""
    lat0 = point[0]
    p = _xy(point[0], point[1], lat0)
    best = None
    for k in range(len(pts) - 1):
        a = _xy(*pts[k], lat0)
        b = _xy(*pts[k + 1], lat0)
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        f = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        q = (a[0] + f * dx, a[1] + f * dy)
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if best is None or d < best[2]:
            best = (k, f, d)
    return best


def dist_m(p, q):
    a, b = _xy(*p, p[0]), _xy(*q, p[0])
    return math.hypot(a[0] - b[0], a[1] - b[1])


# --- párování ---------------------------------------------------------------------------------
def vehicle_point(v):
    c = (v.get("geometry") or {}).get("coordinates") or []
    return (float(c[1]), float(c[0])) if len(c) >= 2 else None


def in_service(v, include_inactive=False):
    name = v.get("name") or ""
    if "NO RECENT DATA" in name.upper():
        return False
    if not v.get("is_visible", True):
        return False
    return include_inactive or bool(v.get("is_active"))


def candidates(v, static, now):
    """Všechny přípustné (spoj, zpoždění_min, úsek, podíl, service_date) pro vozidlo."""
    direction = (v.get("line") or {}).get("_id")
    point = vehicle_point(v)
    if not direction or point is None:
        return []
    today = now.date()
    now_min = now.hour * 60 + now.minute + now.second / 60
    out = []
    # dnešní služba, a krátce po půlnoci i včerejší (časy přes 24:00)
    days = [(today, now_min)]
    if now_min < 240:
        days.append((today - timedelta(days=1), now_min + 1440))
    for trip in static["by_dir"].get(direction, []):
        k, f, d = project(point, trip["pts"])
        if d > MAX_OFF_ROUTE_M:
            continue
        t0, t1 = trip["stops"][k][2], trip["stops"][k + 1][2]
        sched = t0 + f * (t1 - t0)
        for day, nm in days:
            if not service_active(static, trip["service_id"], day):
                continue
            delay = nm - sched
            if MIN_DELAY_MIN <= delay <= MAX_DELAY_MIN:
                out.append((trip, delay, k, f, day))
    return out


def match_all(vehicles, static, now, include_inactive=False, previous=None):
    """
    previous: vozidlo -> (trip_id, service_date) z minulého cyklu. Pokud je minulý
    spoj pořád přípustný, dostane přednost – u hustých linek by se jinak vozidlo
    mezi zprávami překlápělo mezi dva po sobě jdoucí spoje.
    """
    previous = previous or {}
    pairs = []
    for v in vehicles:
        if not in_service(v, include_inactive):
            continue
        prev = previous.get(v["_id"])
        for trip, delay, k, f, day in candidates(v, static, now):
            # menší odchylka má přednost; mírně zvýhodni zpoždění před předjetím
            score = abs(delay) if delay >= 0 else abs(delay) * 1.5
            if prev == (trip["trip_id"], day):
                score -= STICKY_BONUS_MIN
            pairs.append((score, v["_id"], trip, delay, k, f, day))
    pairs.sort(key=lambda x: x[0])
    taken_v, taken_t, result = set(), set(), {}
    for score, vid, trip, delay, k, f, day in pairs:
        key = (trip["trip_id"], day)
        if vid in taken_v or key in taken_t:
            continue
        taken_v.add(vid)
        taken_t.add(key)
        result[vid] = (trip, delay, k, f, day)
    return result


# --- GTFS-RT ------------------------------------------------------------------------------------
def new_feed(ts):
    fm = gtfs_realtime_pb2.FeedMessage()
    fm.header.gtfs_realtime_version = "2.0"
    fm.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    fm.header.timestamp = int(ts)
    return fm


def _fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def route_of_direction(static, direction):
    trips = static["by_dir"].get(direction)
    return trips[0]["route_id"] if trips else None


def build_feeds(vehicles, static, received_at, include_inactive=False, previous=None):
    """previous (dict) se aktualizuje na aktuální přiřazení vozidel ke spojům."""
    now = datetime.fromtimestamp(received_at, TZ)
    vp, tu = new_feed(received_at), new_feed(received_at)
    matches = match_all(vehicles, static, now, include_inactive, previous)
    if previous is not None:
        previous.clear()
        previous.update({vid: (m[0]["trip_id"], m[4]) for vid, m in matches.items()})
    stats = Counter()
    for v in vehicles:
        stats["vozidel"] += 1
        if not in_service(v, include_inactive):
            stats["mimo_provoz"] += 1
            continue
        stats["v_provozu"] += 1
        point = vehicle_point(v)
        if point is None:
            continue
        direction = (v.get("line") or {}).get("_id")
        m = matches.get(v["_id"])
        e = vp.entity.add()
        e.id = v["_id"]
        pos = e.vehicle
        pos.position.latitude, pos.position.longitude = point
        if isinstance(v.get("heading"), (int, float)):
            pos.position.bearing = float(v["heading"]) % 360
        pos.vehicle.id = v["_id"]
        pos.vehicle.label = (v.get("name") or "").strip()
        pos.timestamp = int(received_at)
        if m:
            trip, delay, k, f, day = m
            stats["naparovano"] += 1
            pos.trip.trip_id = trip["trip_id"]
            pos.trip.route_id = trip["route_id"]
            pos.trip.start_date = day.strftime("%Y%m%d")
            pos.trip.start_time = _fmt(trip["stops"][0][2])
            nxt = k + 1 if f > 0 else k
            seq, stop_id, _t = trip["stops"][nxt]
            pos.current_stop_sequence = seq
            pos.stop_id = stop_id
            pos.current_status = (VP.STOPPED_AT if dist_m(point, trip["pts"][nxt]) <= AT_STOP_M
                                  else VP.IN_TRANSIT_TO)

            t = tu.entity.add()
            t.id = trip["trip_id"]
            upd = t.trip_update
            upd.trip.trip_id = trip["trip_id"]
            upd.trip.route_id = trip["route_id"]
            upd.trip.start_date = day.strftime("%Y%m%d")
            upd.trip.start_time = _fmt(trip["stops"][0][2])
            upd.vehicle.id = v["_id"]
            upd.vehicle.label = (v.get("name") or "").strip()
            upd.timestamp = int(received_at)
            delay_s = int(round(delay * 60))
            # stejné zpoždění na všech zastávkách – i na projetých, ať je časová osa
            # monotónní i u spoje jedoucího napřed
            for seq, stop_id, _t in trip["stops"]:
                stu = upd.stop_time_update.add()
                stu.stop_sequence = seq
                stu.stop_id = stop_id
                stu.arrival.delay = delay_s
                stu.departure.delay = delay_s
        else:
            stats["nenaparovano"] += 1
            rid = route_of_direction(static, direction)
            if rid:
                pos.trip.route_id = rid
    return vp, tu, dict(stats)
