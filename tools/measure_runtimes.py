"""
Změří skutečné jízdní doby úseků z opakovaných odpovědí /buses/{id}/upcomingStations.
Průjezd zastávkou = okamžik, kdy se objeví v passed_stations (střed mezi dvěma vzorky).
Doba mezi dvěma průjezdy se rozdělí na úseky poměrem podle odhadu navigace (ETA),
případně podle vzdálenosti. Výstup: travel_times.json
  {směr: {"a>b": {"obs_min": průměr, "n": počet, "eta_min": průměr ETA, "km": vzdálenost}}}
"""
import gzip, json, math, statistics, sys
from collections import defaultdict

samples_files, raw_path, out_path = sys.argv[1:-2], sys.argv[-2], sys.argv[-1]
raw = json.load(gzip.open(raw_path, "rt", encoding="utf-8")) if raw_path.endswith(".gz") else json.load(open(raw_path, encoding="utf-8"))
stations = {s["_id"]: s["geometry"]["coordinates"] for s in raw["stations"]["data"]}
order = {}
for L in raw["zones"]["data"]:
    for k in ("on_line_going", "on_line_return"):
        d = L.get(k)
        if d:
            order[d["_id"]] = [s["_id"] for s in d["ordered_stations"]]

def km(a, b):
    (lo1, la1), (lo2, la2) = stations[a], stations[b]
    return 6371 * math.hypot(math.radians(lo2 - lo1) * math.cos(math.radians(la1)), math.radians(la2 - la1))

rows = []
seen = set()
for f in samples_files:
    for line in open(f, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        key = (round(r["t"], 3), r["vehicle"]["_id"])
        if key in seen or not isinstance(r.get("response"), dict) or not r["response"].get("success"):
            continue
        seen.add(key)
        rows.append(r)

# ETA přírůstky po úsecích
eta = defaultdict(list)
for r in rows:
    up = r["response"]["data"].get("upcoming_stations") or []
    for a, b in zip(up, up[1:]):
        if isinstance(a["distance_to_station_min"], int) and isinstance(b["distance_to_station_min"], int):
            eta[(a["line_id"], a["_id"], b["_id"])].append(b["distance_to_station_min"] - a["distance_to_station_min"])

def weight(d, a, b):
    e = eta.get((d, a, b))
    if e and len(e) >= 3:
        return max(statistics.mean(e), 0.2)
    return max(km(a, b) * 3.5, 0.2)          # 3.5 min/km jako záloha

# průjezdy po vozidlech
byv = defaultdict(list)
for r in rows:
    byv[r["vehicle"]["_id"]].append(r)
obs = defaultdict(list)
spans_used = spans_dropped = 0
for vid, vs in byv.items():
    vs.sort(key=lambda r: r["t"])
    prev = None
    events = []   # (t, index) v rámci jedné jízdy
    def flush():
        global spans_used, spans_dropped
        for (t1, i1, d), (t2, i2, _d) in zip(events, events[1:]):
            if i2 <= i1:
                continue
            ids = order[d]
            segs = list(zip(ids[i1:i2], ids[i1 + 1:i2 + 1]))
            dur = (t2 - t1) / 60
            dist = sum(km(a, b) for a, b in segs)
            # vyřaď stání (pod 3 km/h) a nesmyslně rychlé úseky (nad 70 km/h)
            if dur <= 0 or dist / (dur / 60) < 3 or dist / (dur / 60) > 70 or dur > 45:
                spans_dropped += 1
                continue
            spans_used += 1
            w = [weight(d, a, b) for a, b in segs]
            tot = sum(w)
            for (a, b), wi in zip(segs, w):
                obs[(d, a, b)].append(dur * wi / tot)
    for r in vs:
        d = r["vehicle"]["line_id"]
        if d not in order:
            continue
        passed = [s["_id"] for s in r["response"]["data"].get("passed_stations") or []]
        if prev is not None:
            pd, pp, pt = prev
            if pd != d or not set(pp) <= set(passed):          # nová jízda
                flush(); events = []
            else:
                new = [s for s in passed if s not in pp]
                if new:
                    ids = order[d]
                    idx = max((i for i, s in enumerate(ids) if s in new), default=None)
                    if idx is not None:
                        events.append(((pt + r["t"]) / 2, idx, d))
        prev = (d, passed, r["t"])
    flush()

out = defaultdict(dict)
for d, ids in order.items():
    for a, b in zip(ids, ids[1:]):
        o, e = obs.get((d, a, b)), eta.get((d, a, b))
        if o or e:
            out[d][f"{a}>{b}"] = {"obs_min": round(statistics.median(o), 3) if o else None, "n": len(o) if o else 0,
                                  "eta_min": round(statistics.mean(e), 3) if e else None, "eta_n": len(e) if e else 0,
                                  "km": round(km(a, b), 3)}
json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"vzorků {len(rows)}, vozidel {len(byv)}, použitých intervalů {spans_used}, vyřazených {spans_dropped}")
print(f"úseků se skutečným měřením: {sum(1 for d in out.values() for v in d.values() if v['n'])}, jen s ETA: "
      f"{sum(1 for d in out.values() for v in d.values() if not v['n'] and v['eta_min'] is not None)}")
