"""Breadcrumb track logger — turn the live gpsd fix into a durable path.

WHY THIS EXISTS
The rig always knew where it was; it just never wrote it down. gpsd delivers
lat/lon/alt/speed/track once a second and tmt/gps.py mirrors the whole TPV to a
tmpfs file — where the next fix overwrites it. The only position that survived
was the lat/lon stamped onto a sighting, which lands at whatever cadence a BLE
advert or an SDR sweep happens to fire: of 2,001,431 sightings, 3,076 carried a
usable fix (0.15%), and speed/heading/altitude were never stored at all.

So this module writes the path itself, on the GPS's own cadence, independent of
whether any radio heard anything. That yields trips, distance, duration and
speed directly, and it gives tmt/score.py a continuous observer path to
interpolate against instead of the sparse opportunistic stamps it uses now to
decide position_corroborated vs local_fixture.

TWO THINGS THIS DELIBERATELY REFUSES TO DO
1. Invent distance from GPS wander. A parked receiver jitters by tens of metres
   forever; summing that would grow an odometer overnight in a driveway. This
   is the same failure that credited 76 m of wander as travel in the scorer, so
   distance accrues ONLY while a trip is open and ONLY for per-sample steps
   above NOISE_FLOOR_M. Parked crumbs record distance 0, not noise.
2. Invent distance across a gap. The rig reboots at 04:00 daily and gpsd drops
   out; the fix after a gap may be anywhere. A step spanning more than
   max_gap_seconds contributes no distance rather than a straight line through
   everywhere we were not.

TRIP SEGMENTATION is a Schmitt trigger: a trip opens at move_start_mps and
closes only after stop_seconds continuously below move_stop_mps. Two thresholds
because one threshold chatters — a single value would open and close a trip
repeatedly while creeping in traffic. The stop delay is what keeps a red light
or a fuel stop from chopping one drive into three. Time spent stopped mid-trip
is still inside the trip's duration; it is excluded from moving_s instead, so
"how long did it take" and "how long was I actually driving" stay separable.
"""

import time

from .gps import haversine_m

# Per-sample displacement below this is receiver noise, not travel.
NOISE_FLOOR_M = 3.0

DEFAULTS = {
    "enabled": True,
    "min_dist_m": 25.0,            # crumb spacing while moving (~64 rows/mile)
    "min_heading_deg": 25.0,       # ...but always crumb a real corner
    "min_interval_seconds": 1.0,   # never faster than gpsd's own 1 Hz
    "park_heartbeat_seconds": 30.0,  # parked proof-of-presence ("we did NOT move")
    "move_start_mps": 1.5,         # ~3.4 mph: open a trip
    "move_stop_mps": 0.7,          # ~1.6 mph: candidate for closing one
    "stop_seconds": 180.0,         # ...sustained this long before it closes
    "max_gap_seconds": 300.0,      # beyond this, distance is unknowable
    "flush_seconds": 10.0,         # bound rows at risk without holding the lock
    "max_pending": 20,
}


def heading_delta(a, b):
    """Smallest angle between two true headings, in degrees."""
    if a is None or b is None:
        return 0.0
    return abs((a - b + 180.0) % 360.0 - 180.0)


class TrackLogger:
    """Consume validated fixes, emit breadcrumbs and trip boundaries.

    Feed it from GpsPoller's on_fix, which only fires for fixes that already
    passed fix_problem() and the teleport check — this class trusts positions
    and concerns itself with cadence, distance and trip state.
    """

    def __init__(self, store, cfg=None, clock=time.time):
        c = dict(DEFAULTS)
        c.update(cfg or {})
        self.cfg = c
        self.store = store
        self.clock = clock
        self.enabled = bool(c["enabled"])
        self.min_dist = float(c["min_dist_m"])
        self.min_heading = float(c["min_heading_deg"])
        self.min_interval = float(c["min_interval_seconds"])
        self.park_heartbeat = float(c["park_heartbeat_seconds"])
        self.move_start = float(c["move_start_mps"])
        self.move_stop = float(c["move_stop_mps"])
        self.stop_seconds = float(c["stop_seconds"])
        self.max_gap = float(c["max_gap_seconds"])
        self.flush_seconds = float(c["flush_seconds"])
        self.max_pending = int(c["max_pending"])

        self._crumb = None        # (lat, lon, ts, heading) last row WRITTEN
        self._prev = None         # (lat, lon, ts) last fix SEEN, for path length
        self._acc_dist = 0.0      # true path length accumulated since _crumb
        self._trip = None         # open trip id, or None when parked
        self._below_since = None  # when speed first dropped below move_stop
        self._pending = 0
        self._last_flush = 0.0
        self.n_written = 0

    # -- lifecycle ---------------------------------------------------------
    def resume(self):
        """Re-attach to a trip still open when the poller last stopped.

        Without this, a poller restart mid-drive (or the nightly reboot caught
        on the road) splits one journey into two trips that each under-report.
        Only resume if the last crumb is recent enough that the trip could not
        already have timed out.
        """
        row = self.store.last_track_row()
        if not row or row.get("trip") is None:
            return None
        if self.clock() - row["ts"] > self.stop_seconds:
            return None
        self._trip = row["trip"]
        self._crumb = (row["lat"], row["lon"], row["ts"], row.get("track"))
        self._prev = (row["lat"], row["lon"], row["ts"])
        return self._trip

    def close(self):
        self._flush(force=True)

    # -- the hot path ------------------------------------------------------
    def on_fix(self, fix):
        """Record one validated fix. Returns an event dict, or None."""
        if not self.enabled:
            return None
        ts = fix.get("ts") or self.clock()
        lat, lon = fix["lat"], fix["lon"]
        speed = fix.get("speed")
        heading = fix.get("track")

        # Step since the previous fix: path length, and the speed fallback for
        # receivers that report position but omit velocity.
        step = 0.0
        usable_step = False
        dt_prev = None
        if self._prev is not None:
            plat, plon, pts = self._prev
            dt_prev = ts - pts
            if 0 < dt_prev <= self.max_gap:
                step = haversine_m(plat, plon, lat, lon)
                usable_step = True
                if speed is None:
                    speed = step / dt_prev
        self._prev = (lat, lon, ts)
        if speed is None:
            speed = 0.0

        moving_now = speed >= self.move_stop
        started, ended = False, False
        if self._trip is None:
            if speed >= self.move_start:
                self._trip = int(ts)      # epoch second: unique across restarts
                self._below_since = None
                self._acc_dist = 0.0
                started = True
        elif moving_now:
            self._below_since = None
        elif self._below_since is None:
            self._below_since = ts
        elif ts - self._below_since >= self.stop_seconds:
            ended = True

        # Distance accrues only while actually MOVING -- not merely while a
        # trip is open. A trip stays open through a red light by design, and a
        # stationary receiver keeps jittering, so gating on trip-open alone put
        # 341 m of phantom distance into a 12 km test drive (2.8% long) from
        # two stops totalling four minutes. Both guards are needed: gpsd's
        # Doppler speed is trustworthy at rest where position differencing is
        # not, and the noise floor is the fallback for a receiver that reports
        # no velocity at all. Plus a gap short enough to interpolate honestly.
        if (self._trip is not None and moving_now and usable_step
                and step >= NOISE_FLOOR_M):
            self._acc_dist += step

        if not self._should_write(ts, lat, lon, heading, started, ended):
            self._flush()
            return None

        # dt is normally the span since the previous crumb, but the crumb that
        # OPENS a trip is preceded by a parked heartbeat up to 30 s old, and
        # crediting that standing-still interval to moving_s overstated driving
        # time by 29 s on a 600 s test drive. The first crumb of a trip is worth
        # only the sample interval it actually covers.
        dt = (ts - self._crumb[2]) if self._crumb else None
        if started and dt_prev is not None:
            dt = min(dt, dt_prev) if dt is not None else dt_prev
        dist = None if self._crumb is None else (
            self._acc_dist if self._trip is not None else 0.0)
        trip = self._trip
        self.store.add_track(
            ts=ts, lat=lat, lon=lon, alt=fix.get("alt"), speed=speed,
            track=heading, mode=fix.get("mode"), dist_m=dist, dt=dt,
            moving=1 if moving_now else 0, trip=trip)
        self._crumb = (lat, lon, ts, heading)
        self._acc_dist = 0.0
        self._pending += 1
        self.n_written += 1

        event = None
        if started:
            event = {"event": "trip_start", "trip": trip}
        if ended:
            self._trip = None
            self._below_since = None
            event = {"event": "trip_end", "trip": trip,
                     "summary": self.store.trip_summary(trip)}
        # A trip boundary is the one thing worth paying a lock for immediately;
        # everything else rides the flush cadence.
        self._flush(force=bool(event))
        return event

    def _should_write(self, ts, lat, lon, heading, started, ended):
        if started or ended or self._crumb is None:
            return True
        clat, clon, cts, chdg = self._crumb
        since = ts - cts
        if since < self.min_interval:
            return False
        if self._trip is None:
            return since >= self.park_heartbeat   # parked: prove we stayed put
        return (haversine_m(clat, clon, lat, lon) >= self.min_dist
                or heading_delta(heading, chdg) >= self.min_heading
                or since >= self.park_heartbeat)

    def _flush(self, force=False):
        if not self._pending:
            return
        now = self.clock()
        if (force or self._pending >= self.max_pending
                or now - self._last_flush >= self.flush_seconds):
            self.store.commit()
            self._pending = 0
            self._last_flush = now
