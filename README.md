# Prishtina GTFS Feed 🇽🇰

Automatically generated GTFS and GTFS-Realtime data for city and suburban buses in Prishtina operated by Trafiku Urban, built from the data used by the Trafiku Urban app. The static feed is updated every day.

## Feeds

| Feed | URL |
|---|---|
| GTFS (static) | [`prishtina_gtfs.zip`](https://github.com/hoermalmeister/prishtina_gtfs/raw/main/prishtina_gtfs.zip) |
| GTFS-RT Vehicle Positions | `https://<railway-domain>/vehicle_positions.pb` |
| GTFS-RT Trip Updates | `https://<railway-domain>/trip_updates.pb` |

## How the static feed is built

The source API (`https://tu.appbites.dev/api/public`) provides lines with directions and ordered stops (`/zones`), stops (`/stations`) and, for every direction–stop pair, a list of departures for weekdays and weekends (`/schedules/{direction}/{stop}`). It does not provide trips.

- `fetch.py` downloads everything into `data/raw.json.gz` (about 1,700 requests, run daily by GitHub Actions).
- `build_gtfs.py` assembles trips by aligning the departure lists along each direction: buses do not overtake each other, so the k-th departure at one stop corresponds to the k-th departure at the next one, with gaps where trips start or end.
- Only stops that have a timetable are used in `stop_times.txt`. Stops without times (for example during a diversion) are kept in `stops.txt` as reserve stops without trips. Directions with times at fewer than two stops have no trips.
- Service: `week` = Monday–Friday, `weekend` = Saturday–Sunday (public holidays are not available in the source data).
- Source data fixes: stops with obviously wrong coordinates are moved to the midpoint of their neighbours on the route; a direction listed twice for the same line (7C) is used once. Both are reported in the build log.

## Realtime

`realtime/` is a GTFS-RT server for Railway. It keeps a socket.io connection to `wss://tu.appbites.dev` (path `/ws/socket.io`, event `bus_update`, the full vehicle list about every 3 seconds). The messages contain position, heading, line and direction, but no trip, so the trip is determined from the position: the vehicle is projected onto the route of each trip of its direction running today, the scheduled time at that point is interpolated from the timetable, and the trip with the smallest deviation (−10 to +40 minutes) is chosen. Each trip gets at most one vehicle, and a vehicle keeps its previous trip while it remains plausible. Only vehicles with `is_active` and without "NO RECENT DATA" are used.

- **Vehicle Positions** – all vehicles in service (with trip when matched)
- **Trip Updates** – matched trips; the current deviation is applied to all stops of the trip

### Railway

1. **+ New → GitHub Repo → `prishtina_gtfs`**.
2. **Settings**: builder Railpack, start command `python realtime/server.py`, healthcheck path `/healthz`; Watch Paths `/realtime/**`, `/requirements.txt`.
3. **Networking → Generate Domain**.

Optional variables: `BUILD_INTERVAL` (10 s), `STATIC_REFRESH_HOURS` (12), `INCLUDE_INACTIVE` (0), `STATIC_GTFS_URL`.

Endpoints: `/vehicle_positions.pb`, `/trip_updates.pb`, `.json` variants, `/status`, `/debug/vehicles.json` (last raw `bus_update`), `/healthz`.

## License

This data is provided under the **CC BY-NC-ND 4.0** license.

[![CC BY-NC-ND 4.0](https://i.creativecommons.org/l/by-nc-nd/4.0/88x31.png)](http://creativecommons.org/licenses/by-nc-nd/4.0/)

* **BY (Attribution):** You must give appropriate credit to the author.
* **NC (NonCommercial):** You may not use the material for commercial purposes.
* **ND (NoDerivatives):** If you remix, transform, or build upon the material, you may not distribute the modified material.
