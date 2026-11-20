"""
Sestaví GTFS pro MHD Priština (Trafiku Urban) z dat aplikace:
  /zones      – linky, směry a pořadí zastávek
  /stations   – zastávky se souřadnicemi
  /schedules  – odjezdy pro každou dvojici směr–zastávka (všední den / víkend)

API dává jen seznamy odjezdů po zastávkách, ne spoje. Spoje se skládají
zarovnáním seznamů po trase (autobusy se nepředjíždějí, k-tý odjezd na jedné
zastávce odpovídá k-tému na další, s mezerami tam, kde spoj začíná/končí).

Do stop_times jdou jen zastávky s jízdním řádem. Zastávky bez časů (např. při
výluce) zůstávají jen v stops.txt jako rezerva. Směry, které mají časy na méně
než dvou zastávkách, spoje nemají.

Použití: python build_gtfs.py [data/raw.json.gz] [prishtina_gtfs.zip]
"""

import csv
import gzip
import io
import json
import math
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RAW = sys.argv[1] if len(sys.argv) > 1 else "data/raw.json.gz"
OUT_ZIP = sys.argv[2] if len(sys.argv) > 2 else "prishtina_gtfs.zip"
TZ = "Europe/Belgrade"            # Kosovo používá středoevropský čas
VALID_DAYS = 60
NIGHT_CUTOFF = 180                # časy před 3:00 patří k předchozímu provoznímu dni
MIN_TOL, REL_TOL = 3, 0.5         # tolerance napojení: max(3 min, 50 % jízdní doby)

AGENCY = {"agency_id": "TU", "agency_name": "Trafiku Urban Prishtinë",
          "agency_url": "https://trafikurban-pr.com", "agency_timezone": TZ,
          "agency_lang": "sq"}

SERVICES = {"week": ("WEEKDAY", [1, 1, 1, 1, 1, 0, 0]),
            "weekend": ("WEEKEND", [0, 0, 0, 0, 0, 1, 1])}


def to_min(t):
    h, m = map(int, t.split(":"))
    v = h * 60 + m
    return v + 1440 if v < NIGHT_CUTOFF else v


def clean(times):
    return sorted({to_min(t) for t in times})


def dist_km(a, b):
    (lon1, lat1), (lon2, lat2) = a, b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371.0 * math.hypot(x, y)


def fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def align(la, lb, travel, gap):
    """
    Zarovná dva seřazené seznamy časů (odjezdy na zastávce A a B) se zachováním
    pořadí – autobusy se nepředjíždějí. Shoda a->b stojí |b - a - travel|
    (povolena jen pro 0 <= b - a <= 3*travel + 10), vynechání stojí gap.
    Vrátí seznam dvojic (i, j) shodných indexů.
    """
    n, m = len(la), len(lb)
    INF = float("inf")
    lo_ok = 0
    hi_ok = 3 * travel + 10
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = i * gap
        back[i][0] = 1
    for j in range(1, m + 1):
        cost[0][j] = j * gap
        back[0][j] = 2
    for i in range(1, n + 1):
        a = la[i - 1]
        ci, cp = cost[i], cost[i - 1]
        bi = back[i]
        for j in range(1, m + 1):
            best, how = cp[j] + gap, 1                 # A[i] bez páru
            c = ci[j - 1] + gap
            if c < best:
                best, how = c, 2                       # B[j] bez páru
            d = lb[j - 1] - a
            if lo_ok <= d <= hi_ok:
                c = cp[j - 1] + abs(d - travel)
                if c < best:
                    best, how = c, 3                   # shoda
            ci[j] = best
            bi[j] = how
    pairs, i, j = [], n, m
    while i > 0 or j > 0:
        how = back[i][j]
        if how == 3:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif how == 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def chain(timepoints, lists, repeated=frozenset()):
    """
    timepoints: indexy zastávek s jízdním řádem (vzestupně)
    lists: index -> seřazený seznam minut
    repeated: indexy zastávek, které se na trase opakují (API vrací sloučený seznam
              obou průjezdů) – na nich se nové spoje nezakládají.
    Vrátí seznam spojů, každý = dict index -> minuta (jen skutečné časy).
    """
    trips = [{"known": {timepoints[0]: t}, "cur": t} for t in lists[timepoints[0]]]
    for a, b in zip(timepoints, timepoints[1:]):
        lb = lists[b]
        active = sorted((tr for tr in trips if tr["cur"] is not None), key=lambda tr: tr["cur"])
        la = [tr["cur"] for tr in active]
        diffs = []
        for t in la:
            nxt = [x - t for x in lb if x >= t]
            if nxt:
                diffs.append(min(nxt))
        if not diffs:
            continue
        travel = max(statistics.median(diffs), 0.5)
        gap = max(MIN_TOL * 2, travel)
        pairs = align(la, lb, travel, gap)
        matched_a = {i for i, _ in pairs}
        matched_b = {j for _, j in pairs}
        for i, j in pairs:
            active[i]["known"][b] = lb[j]
            active[i]["cur"] = lb[j]
        for i, tr in enumerate(active):
            if i not in matched_a:
                tr["cur"] = tr["cur"] + travel          # pokračuj odhadem
        if b not in repeated:
            for j, x in enumerate(lb):
                if j not in matched_b:
                    trips.append({"known": {b: x}, "cur": x})
    return [tr["known"] for tr in trips]


def fix_outlier_coords(lines, coord, stations):
    """
    Zastávky se zjevně chybnými souřadnicemi (daleko od obou sousedů na trase,
    přičemž sousedé jsou blízko sebe) dostanou polohu uprostřed mezi sousedy.
    """
    fixes = {}
    for line in lines:
        for key in ("on_line_going", "on_line_return"):
            d = line.get(key)
            if not d:
                continue
            ids = [s["_id"] for s in d["ordered_stations"] if s["_id"] in coord]
            for i in range(1, len(ids) - 1):
                p, c, n = coord[ids[i - 1]], coord[ids[i]], coord[ids[i + 1]]
                a, b, pn = dist_km(p, c), dist_km(c, n), dist_km(p, n)
                if a > 2.5 and b > 2.5 and pn < 0.6 * (a + b) and ids[i] not in fixes:
                    fixes[ids[i]] = [(p[0] + n[0]) / 2, (p[1] + n[1]) / 2]
    for sid, xy in fixes.items():
        print(f"Oprava souřadnic: {stations[sid]['name'].strip()} {coord[sid]} -> "
              f"[{xy[0]:.6f}, {xy[1]:.6f}] (střed mezi sousedními zastávkami)")
        coord[sid] = xy


def text_color(hex_color):
    h = hex_color.lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "FFFFFF"
    return "000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "FFFFFF"


def main():
    opener = gzip.open if RAW.endswith(".gz") else open
    with opener(RAW, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    stations = {s["_id"]: s for s in raw["stations"]["data"]}
    lines = raw["zones"]["data"]
    sched = {k: v for k, v in raw["schedules"].items() if isinstance(v, dict)}

    coord = {sid: list(st["geometry"]["coordinates"]) for sid, st in stations.items()}
    fix_outlier_coords(lines, coord, stations)

    dirs = []
    seen_dirs = set()
    for line in lines:
        for key, dir_id in (("on_line_going", 0), ("on_line_return", 1)):
            d = line.get(key)
            if not d or not line.get("is_active", True):
                continue
            if d["_id"] in seen_dirs:      # zdroj má u některých linek (7C) stejný směr tam i zpět
                print(f"Varování: linka {line['bus_sign']} má směr {d['_id']} vícekrát – použit jen jednou")
                continue
            seen_dirs.add(d["_id"])
            stops = [s["_id"] for s in d["ordered_stations"] if s["_id"] in stations]
            dirs.append((line, d, dir_id, stops))

    routes, trips_out, st_out = {}, [], []
    stats = Counter()
    for line, d, dir_id, stops in dirs:
        rid = f"R_{line['_id']}"
        for kind, (sid, _days) in SERVICES.items():
            lists = {i: clean(sched[f"{d['_id']}/{s}"][kind]) for i, s in enumerate(stops)
                     if f"{d['_id']}/{s}" in sched and sched[f"{d['_id']}/{s}"].get(kind)}
            if len(lists) < 2:
                stats[f"směr s časy na < 2 zastávkách ({kind})"] += 1
                continue
            routes[rid] = line
            tps = sorted(lists)
            rep = frozenset(i for i, x in enumerate(stops) if stops.count(x) > 1)
            chained = chain(tps, lists, rep)
            n = 0
            for known in sorted(chained, key=lambda tr: min(tr.values())):
                if len(known) < 2:
                    stats["zahozený spoj s jediným časem"] += 1
                    continue
                idx = sorted(known)
                if any(known[b] < known[a] for a, b in zip(idx, idx[1:])):
                    stats["zahozený spoj s klesajícími časy"] += 1
                    continue
                n += 1
                tid = f"{d['_id']}_{sid}_{n:03d}"
                headsign = " ".join(stations[stops[idx[-1]]]["name"].split())
                trips_out.append([rid, sid, tid, headsign, dir_id])
                for i in idx:
                    st_out.append([tid, fmt(known[i]), fmt(known[i]), stops[i], i + 1])
                stats["spojů"] += 1

    today = datetime.now(ZoneInfo(TZ)).date()
    end = today + timedelta(days=VALID_DAYS)

    def w(zf, name, header, rows):
        buf = io.StringIO()
        wr = csv.writer(buf, lineterminator="\n")
        wr.writerow(header)
        wr.writerows(rows)
        zf.writestr(name, buf.getvalue())

    def natural(line):
        s = line["bus_sign"]
        num = "".join(ch for ch in s if ch.isdigit())
        return (s[0].isalpha(), int(num) if num else 0, s)

    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        w(zf, "agency.txt", list(AGENCY), [list(AGENCY.values())])
        w(zf, "routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name",
                             "route_type", "route_color", "route_text_color"],
          [[rid, "TU", l["bus_sign"].strip(), (l["on_line_going"] or {}).get("description", l["name"]).strip(), 3,
            (l.get("color") or "#888888").lstrip("#").upper(), text_color(l.get("color") or "#888888")]
           for rid, l in sorted(routes.items(), key=lambda kv: natural(kv[1]))])
        w(zf, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
          [[s, " ".join(stations[s]["name"].split()), round(coord[s][1], 7), round(coord[s][0], 7)]
           for s in sorted(stations)])              # všechny zastávky, i rezervní bez spojů
        w(zf, "calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                               "saturday", "sunday", "start_date", "end_date"],
          [[sid, *days, today.strftime("%Y%m%d"), end.strftime("%Y%m%d")] for sid, days in SERVICES.values()])
        w(zf, "trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"], trips_out)
        w(zf, "stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id",
                                 "stop_sequence"], st_out)
        w(zf, "feed_info.txt", ["feed_publisher_name", "feed_publisher_url", "feed_lang",
                                "feed_start_date", "feed_end_date", "feed_version", "feed_contact_url"],
          [["Petr Novak", "https://github.com/hoermalmeister/prishtina_gtfs", "sq",
            today.strftime("%Y%m%d"), end.strftime("%Y%m%d"), today.strftime("%Y%m%d"),
            "https://github.com/hoermalmeister/prishtina_gtfs/issues"]])

    used = {r[3] for r in st_out}
    print(f"Hotovo {OUT_ZIP}: linek {len(routes)}, spojů {len(trips_out)}, "
          f"zastávek {len(stations)} (se spoji {len(used)}, rezervních {len(stations) - len(used)})")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
