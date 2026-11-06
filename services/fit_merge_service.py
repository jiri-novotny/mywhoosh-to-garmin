"""Merge MyWhoosh virtual-ride data into a watch recording of the same ride.

The watch FIT file is the base: device info, sensors, laps, training effect,
training load, respiration, zones etc. stay exactly as the watch recorded them
(the file is rewritten losslessly by services.fit_codec). From the MyWhoosh
file(s) we take what the watch cannot know on a trainer:

- GPS position and altitude of the virtual route
- virtual speed and distance (instead of the trainer's estimate)

One watch recording may contain several MyWhoosh rides (routes ridden back to
back); each is aligned to the watch clock separately. Outside of MyWhoosh rides
(e.g. warm-up) the watch's own speed is kept and distance continues from it.
"""

import logging
import os
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from services.fit_codec import (DataMessage, FitData, ENUM, SINT32, UINT16,
                                UINT32, read_fit, write_fit)

# Global message numbers
SPORT, SESSION, LAP, RECORD = 12, 18, 19, 20

# Record fields
R_LAT, R_LONG, R_ALT, R_HR, R_CAD, R_DIST, R_SPEED, R_POWER = 0, 1, 2, 3, 4, 5, 6, 7
R_ENH_SPEED, R_ENH_ALT = 73, 78
TS = 253

SUB_SPORT_VIRTUAL = 58

# Session / lap fields that depend on distance or position: name -> (session num, lap num, base type)
SUMMARY_FIELDS = {
    'start_lat': (3, 3, SINT32),
    'start_long': (4, 4, SINT32),
    'end_lat': (None, 5, SINT32),
    'end_long': (None, 6, SINT32),
    'total_distance': (9, 9, UINT32),        # scale 100
    'avg_speed': (14, 13, UINT16),           # scale 1000
    'max_speed': (15, 14, UINT16),           # scale 1000
    'total_ascent': (22, 21, UINT16),
    'total_descent': (23, 22, UINT16),
    'nec_lat': (29, None, SINT32),
    'nec_long': (30, None, SINT32),
    'swc_lat': (31, None, SINT32),
    'swc_long': (32, None, SINT32),
    'enh_avg_speed': (124, 110, UINT32),     # scale 1000
    'enh_max_speed': (125, 111, UINT32),     # scale 1000
}
SUB_SPORT_FIELD = {SESSION: 6, LAP: 39, SPORT: 1}


@dataclass
class VirtualSample:
    lat: Optional[int]       # semicircles
    long: Optional[int]
    alt_raw: Optional[int]   # enhanced_altitude raw (scale 5, offset 500)
    dist_m: Optional[float]
    speed_ms: Optional[float]


@dataclass
class VirtualRide:
    """MyWhoosh samples keyed by *watch* timestamp (clock offset applied)."""
    samples: Dict[int, VirtualSample]
    start: int
    end: int
    offset: int


class FitMergeService:
    """Merge MyWhoosh FIT data into a watch FIT file."""

    def __init__(self, max_clock_offset: int = 60, set_virtual_sub_sport: bool = True):
        """
        Args:
            max_clock_offset: Max seconds the MyWhoosh clock may differ from the watch clock
            set_virtual_sub_sport: Mark session/laps as virtual_activity so Garmin
                Connect shows the ride as "Virtual Cycling" with a map
        """
        self.max_clock_offset = max_clock_offset
        self.set_virtual_sub_sport = set_virtual_sub_sport
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------ public

    def merge(self, watch_fit_path: str, mywhoosh_fit_paths: Sequence[str],
              output_path: Optional[str] = None) -> str:
        """Merge one or more MyWhoosh rides into a watch recording.

        Returns:
            Path of the merged FIT file
        """
        watch = read_fit(watch_fit_path)
        watch_records = [m for m in watch.messages(RECORD) if m.timestamp is not None]
        if not watch_records:
            raise ValueError(f"No records in watch file {watch_fit_path}")

        rides = [self._load_virtual_ride(p, watch_records) for p in mywhoosh_fit_paths]
        rides.sort(key=lambda r: r.start)
        for p, r in zip(mywhoosh_fit_paths, rides):
            self.logger.info(f"MyWhoosh ride {os.path.basename(p)}: clock offset {r.offset:+d}s, "
                             f"{len(r.samples)} samples")

        track = self._rewrite_records(watch_records, rides)
        self._update_summaries(watch, track)

        if output_path is None:
            output_path = os.path.join(tempfile.gettempdir(),
                                       "merged_" + os.path.basename(watch_fit_path))
        write_fit(watch, output_path)
        self.logger.info(f"Merged FIT file saved to {output_path}")
        return output_path

    # ------------------------------------------------------------- MyWhoosh in

    def _load_virtual_ride(self, path: str, watch_records: List[DataMessage]) -> VirtualRide:
        fit = read_fit(path)
        mw_records = [m for m in fit.messages(RECORD) if m.timestamp is not None]
        if not mw_records:
            raise ValueError(f"No records in MyWhoosh file {path}")

        offset = self._estimate_clock_offset(mw_records, watch_records)
        samples = {}
        for m in mw_records:
            dist = m.get(R_DIST)
            speed = m.get(R_ENH_SPEED)
            if speed is None:
                speed = m.get(R_SPEED)
            alt = m.get(R_ENH_ALT)
            if alt is None:
                alt = m.get(R_ALT)
            samples[m.timestamp + offset] = VirtualSample(
                lat=m.get(R_LAT), long=m.get(R_LONG), alt_raw=alt,
                dist_m=None if dist is None else dist / 100.0,
                speed_ms=None if speed is None else speed / 1000.0)
        times = sorted(samples)
        return VirtualRide(samples, times[0], times[-1], offset)

    def _estimate_clock_offset(self, mw_records, watch_records) -> int:
        """Find offset (watch_t = mywhoosh_t + offset) that best aligns HR and power."""
        def series(records):
            return {m.timestamp: (m.get(R_HR), m.get(R_POWER)) for m in records}

        mw, watch = series(mw_records), series(watch_records)
        best: Optional[Tuple[float, int]] = None
        for offset in range(-self.max_clock_offset, self.max_clock_offset + 1):
            hr_err = pw_err = 0.0
            n_hr = n_pw = 0
            for t, (hr, pw) in mw.items():
                other = watch.get(t + offset)
                if other is None:
                    continue
                if hr and other[0]:
                    hr_err += abs(hr - other[0])
                    n_hr += 1
                if pw is not None and other[1] is not None:
                    pw_err += abs(pw - other[1])
                    n_pw += 1
            if n_hr + n_pw < 60:
                continue
            cost = (hr_err / n_hr if n_hr else 0) + (pw_err / n_pw / 10 if n_pw else 0)
            if best is None or cost < best[0]:
                best = (cost, offset)
        if best is None:
            self.logger.warning("Could not align MyWhoosh and watch clocks; assuming offset 0")
            return 0
        return best[1]

    # ----------------------------------------------------------------- records

    @staticmethod
    def _sample_at(ride: VirtualRide, t: int) -> Optional[VirtualSample]:
        sample = ride.samples.get(t)
        if sample is None:  # small gaps: nearest sample within 2 s
            for d in (1, -1, 2, -2):
                sample = ride.samples.get(t + d)
                if sample is not None:
                    break
        return sample

    def _rewrite_records(self, records: List[DataMessage], rides: List[VirtualRide]) -> List[dict]:
        """Write position/altitude/speed/distance into watch records.

        Returns the resulting track (one dict per record) for summary computation.
        """
        track = []
        distance = 0.0
        prev_watch_dist: Optional[float] = None
        ride_idx = 0
        ride_base: Optional[float] = None  # merged distance when the current ride started

        for m in records:
            t = m.timestamp
            watch_dist = m.get(R_DIST)
            watch_dist = None if watch_dist is None else watch_dist / 100.0
            watch_speed = m.get(R_ENH_SPEED)
            speed = None if watch_speed is None else watch_speed / 1000.0

            while ride_idx < len(rides) and t > rides[ride_idx].end:
                ride_idx += 1
                ride_base = None
            ride = rides[ride_idx] if ride_idx < len(rides) and t >= rides[ride_idx].start else None
            sample = self._sample_at(ride, t) if ride else None

            if sample is not None and sample.dist_m is not None:
                if ride_base is None:  # continue from the distance reached so far
                    ride_base = distance - sample.dist_m
                distance = max(distance, ride_base + sample.dist_m)
                if sample.speed_ms is not None:
                    speed = sample.speed_ms
            elif watch_dist is not None and prev_watch_dist is not None:
                distance += max(0.0, watch_dist - prev_watch_dist)
            if watch_dist is not None:
                prev_watch_dist = watch_dist

            m.set(R_LAT, SINT32, sample.lat if sample else None)
            m.set(R_LONG, SINT32, sample.long if sample else None)
            m.set(R_ENH_ALT, UINT32, sample.alt_raw if sample else None)
            m.set(R_DIST, UINT32, round(distance * 100))
            m.set(R_ENH_SPEED, UINT32, None if speed is None else round(speed * 1000))

            track.append({
                't': t, 'dist': distance, 'speed': speed,
                'lat': sample.lat if sample else None,
                'long': sample.long if sample else None,
                'alt': None if not sample or sample.alt_raw is None else sample.alt_raw / 5.0 - 500,
            })
        return track

    # --------------------------------------------------------------- summaries

    def _update_summaries(self, fit: FitData, track: List[dict]) -> None:
        times = [p['t'] for p in track]

        def window(start: int, end: int) -> Tuple[float, List[dict]]:
            """Distance reached before `start`, and the track points within [start, end]."""
            i = bisect_left(times, start)
            return (track[i - 1]['dist'] if i else 0.0), track[i:bisect_left(times, end + 1)]

        for msg in fit.messages():
            if self.set_virtual_sub_sport and msg.global_num in SUB_SPORT_FIELD:
                msg.set(SUB_SPORT_FIELD[msg.global_num], ENUM, SUB_SPORT_VIRTUAL)
            if msg.global_num not in (SESSION, LAP):
                continue
            start = msg.get(2)               # start_time
            elapsed = msg.get(7)             # total_elapsed_time, scale 1000
            if start is None or elapsed is None:
                continue
            dist_before, points = window(start, start + elapsed // 1000)
            if not points:
                continue
            self._apply_summary(msg, points, points[-1]['dist'] - dist_before,
                                is_session=msg.global_num == SESSION)

    def _apply_summary(self, msg: DataMessage, points: List[dict], distance: float,
                       is_session: bool) -> None:
        col = 0 if is_session else 1
        timer = msg.get(8)  # total_timer_time, scale 1000
        speeds = [p['speed'] for p in points if p['speed'] is not None]
        avg_speed = distance / (timer / 1000.0) if timer else None
        max_speed = max(speeds) if speeds else None
        positioned = [p for p in points if p['lat'] is not None and p['long'] is not None]
        ascent, descent = self._ascent_descent([p['alt'] for p in points if p['alt'] is not None])

        values = {
            'total_distance': round(distance * 100),
            'avg_speed': None if avg_speed is None else min(round(avg_speed * 1000), 0xFFFE),
            'enh_avg_speed': None if avg_speed is None else round(avg_speed * 1000),
            'max_speed': None if max_speed is None else min(round(max_speed * 1000), 0xFFFE),
            'enh_max_speed': None if max_speed is None else round(max_speed * 1000),
        }
        if positioned:
            values.update({
                'start_lat': positioned[0]['lat'], 'start_long': positioned[0]['long'],
                'end_lat': positioned[-1]['lat'], 'end_long': positioned[-1]['long'],
                'nec_lat': max(p['lat'] for p in positioned),
                'nec_long': max(p['long'] for p in positioned),
                'swc_lat': min(p['lat'] for p in positioned),
                'swc_long': min(p['long'] for p in positioned),
                'total_ascent': round(ascent), 'total_descent': round(descent),
            })
        for name, value in values.items():
            num = SUMMARY_FIELDS[name][col]
            if num is not None:
                msg.set(num, SUMMARY_FIELDS[name][2], value)

    @staticmethod
    def _ascent_descent(altitudes: List[float]) -> Tuple[float, float]:
        """Total ascent/descent. Virtual altitude is noise-free, so no smoothing
        is applied (this matches the totals MyWhoosh itself reports)."""
        diffs = [b - a for a, b in zip(altitudes, altitudes[1:])]
        return sum(d for d in diffs if d > 0), -sum(d for d in diffs if d < 0)
