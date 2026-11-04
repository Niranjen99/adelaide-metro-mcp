# mini_gtfs test fixture

A tiny, made-up GTFS feed used by the tests. It is not real Adelaide Metro data.
Column headers match the real Adelaide Metro files (including columns the server ignores).

- 3 stops (S1, S2, S3), 2 routes (bus 100, tram T1), 5 trips.
- Service WK runs Mon-Fri, WE runs Sat-Sun, 2026-01-01 to 2027-12-31.
- 2026-12-25 (a Friday): WK is removed and WE is added (holiday timetable).
- Trip T4 runs after midnight (24:50 and 25:10) to test service-day times.
- Trip T5 (weekdays, 07:30 at S1) is drop-off only at S2 (pickup_type 1), so it must not be listed as a departure from S2.
- S3 is the last stop of every trip, so it has no departures at all.
