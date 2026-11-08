"""Tests for the lossless FIT codec and the MyWhoosh + watch merge."""

import pytest
from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import FileType, Sport, SubSport

from services.fit_codec import read_fit, write_fit, UINT32, FIT_EPOCH_OFFSET
from services.fit_merge_service import FitMergeService, RECORD, SESSION, LAP

T0 = 1_790_000_000  # unix seconds


def _hr(t):
    return 100 + (t * 7) % 40   # varying signal so clock alignment is unambiguous


def _power(t):
    return 150 + (t * 13) % 90


def _make_watch(path, duration=120):
    """Watch recording: trainer distance at 5 m/s, no GPS."""
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = 1
    fid.product = 4575
    fid.serial_number = 123
    fid.time_created = T0 * 1000
    b.add(fid)
    for i in range(duration):
        r = RecordMessage()
        r.timestamp = (T0 + i) * 1000
        r.heart_rate = _hr(i)
        r.power = _power(i)
        r.distance = 5.0 * i
        r.enhanced_speed = 5.0
        b.add(r)
    for msg in (LapMessage(), SessionMessage()):
        msg.timestamp = (T0 + duration) * 1000
        msg.start_time = T0 * 1000
        msg.total_elapsed_time = duration
        msg.total_timer_time = duration
        msg.sport = Sport.CYCLING
        msg.sub_sport = SubSport.INDOOR_CYCLING
        b.add(msg)
    b.build().to_file(str(path))


def _make_mywhoosh(path, start_offset, duration, clock_skew):
    """MyWhoosh ride starting `start_offset` s into the watch recording, at 8 m/s,
    with a climbing virtual route. Its clock runs `clock_skew` s ahead of the watch."""
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = 331
    fid.product = 1
    fid.time_created = (T0 + start_offset + clock_skew) * 1000
    b.add(fid)
    for k in range(duration):
        i = start_offset + k  # watch-relative second
        r = RecordMessage()
        r.timestamp = (T0 + i + clock_skew) * 1000
        r.heart_rate = _hr(i)
        r.power = _power(i)
        r.distance = 8.0 * k
        r.enhanced_speed = 8.0
        r.position_lat = 46.0 + k * 1e-4
        r.position_long = 14.0 + k * 1e-4
        r.enhanced_altitude = 100.0 + k * 0.5
        b.add(r)
    b.build().to_file(str(path))


def test_codec_round_trip_is_byte_identical(tmp_path):
    src, out = tmp_path / "w.fit", tmp_path / "out.fit"
    _make_watch(src)
    write_fit(read_fit(str(src)), str(out))
    assert src.read_bytes() == out.read_bytes()


def test_codec_appends_new_field_with_new_definition(tmp_path):
    src, out = tmp_path / "w.fit", tmp_path / "out.fit"
    _make_watch(src)
    fit = read_fit(str(src))
    for m in fit.messages(RECORD):
        m.set(78, UINT32, (200 + 500) * 5)  # enhanced_altitude 200 m
    write_fit(fit, str(out))

    records = [r.message for r in FitFile.from_file(str(out)).records
               if isinstance(r.message, RecordMessage)]
    assert len(records) == 120
    assert all(abs(r.enhanced_altitude - 200) < 1e-6 for r in records)
    assert records[10].heart_rate == _hr(10)  # existing fields untouched


def test_merge_aligns_clock_and_writes_route(tmp_path):
    watch, mw, out = tmp_path / "w.fit", tmp_path / "m.fit", tmp_path / "merged.fit"
    _make_watch(watch, duration=120)
    _make_mywhoosh(mw, start_offset=10, duration=100, clock_skew=2)

    FitMergeService().merge(str(watch), [str(mw)], str(out))
    fit = read_fit(str(out))
    records = fit.messages(RECORD)

    with_gps = [r for r in records if r.get(0) is not None]
    # Ride covers watch seconds 10..109 once the 2 s skew is removed.
    assert [r.timestamp - (T0 - FIT_EPOCH_OFFSET) for r in (with_gps[0], with_gps[-1])] == [10, 109]
    assert records[50].get(3) == _hr(50)                      # watch HR kept

    distances = [r.get(5) / 100 for r in records]
    assert distances == sorted(distances)                     # monotonic
    assert distances[9] == pytest.approx(45.0)                # warm-up: trainer 5 m/s
    assert distances[109] == pytest.approx(45.0 + 8.0 * 99)   # ride: virtual 8 m/s
    assert distances[119] == pytest.approx(45.0 + 8.0 * 99 + 50.0)

    session = fit.messages(SESSION)[0]
    assert session.get(9) / 100 == pytest.approx(distances[-1])   # total_distance
    assert session.get(6) == 58                                    # sub_sport virtual_activity
    assert session.get(22) == pytest.approx(49.5, abs=1)           # total_ascent
    assert session.get(3) is not None                              # start position
    assert fit.messages(LAP)[0].get(39) == 58


def test_merge_two_rides_in_one_recording(tmp_path):
    watch, out = tmp_path / "w.fit", tmp_path / "merged.fit"
    m1, m2 = tmp_path / "m1.fit", tmp_path / "m2.fit"
    _make_watch(watch, duration=200)
    _make_mywhoosh(m1, start_offset=5, duration=80, clock_skew=-1)
    _make_mywhoosh(m2, start_offset=100, duration=90, clock_skew=-1)

    FitMergeService().merge(str(watch), [str(m2), str(m1)], str(out))
    records = read_fit(str(out)).messages(RECORD)

    distances = [r.get(5) / 100 for r in records]
    assert distances == sorted(distances)
    gps = [i for i, r in enumerate(records) if r.get(0) is not None]
    assert gps[0] == 5 and gps[-1] == 189
    assert all(records[i].get(0) is None for i in range(85, 100))  # gap between rides
    # warm-up 4 s @5 + ride 1 79 s @8 + gap 15 s @5 + ride 2 89 s @8 + cool-down 10 s @5
    assert distances[-1] == pytest.approx(20 + 79 * 8 + 15 * 5 + 89 * 8 + 10 * 5)


# --- processor / Garmin helpers --------------------------------------------

from datetime import datetime, timezone  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from services.activity_processor import ActivityProcessor  # noqa: E402
from services.garmin_service import GarminService  # noqa: E402


def test_mywhoosh_window_and_name():
    ride = {'startDatetime': '2026-10-30T04:33:42.000Z', 'rideDuration': '00:27:26.000',
            'title': "MyWhoosh - The Puncher's Playground"}
    start, end = ActivityProcessor._mywhoosh_window(ride)
    assert start == datetime(2026, 10, 30, 4, 33, 42, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 27 * 60 + 26
    other = {'title': 'MyWhoosh - Heian Samurai Circuit'}
    assert ActivityProcessor._merged_name([other, ride]) == \
        "MyWhoosh - Heian Samurai Circuit + The Puncher's Playground"


def test_find_overlapping_activities_matches_by_overlap():
    svc = GarminService('u', 'p', tokenstore='/nonexistent')
    svc._authenticated = True
    svc.client = MagicMock()
    svc.client.get_activities_by_date.return_value = [
        {'activityId': 1, 'startTimeGMT': '2026-10-30 04:11:32', 'elapsedDuration': 3030,
         'activityType': {'typeKey': 'indoor_cycling'}},
        {'activityId': 2, 'startTimeGMT': '2026-10-30 04:30:00', 'elapsedDuration': 1800,
         'activityType': {'typeKey': 'walking'}},
        {'activityId': 3, 'startTimeGMT': '2026-10-30 05:10:00', 'elapsedDuration': 1800,
         'activityType': {'typeKey': 'cycling'}},
    ]
    # Second ride inside the recording: starts 22 min after the watch started.
    start = datetime(2026, 10, 30, 4, 33, 42, tzinfo=timezone.utc)
    end = datetime(2026, 10, 30, 5, 1, 8, tzinfo=timezone.utc)
    found = svc.find_overlapping_activities(start, end)
    assert [a['activityId'] for a in found] == [1]
    assert svc.find_overlapping_activities(start, end, exclude_ids=[1]) == []


def _garmin_with(activities):
    svc = GarminService('u', 'p', tokenstore='/nonexistent')
    svc._authenticated = True
    svc.client = MagicMock()
    svc.client.get_activities_by_date.return_value = activities
    return svc


WATCH_NOV3 = {'activityId': 24947066353, 'activityName': 'Indoor Cycling',
              'startTimeGMT': '2026-11-03 04:07:54', 'startTimeLocal': '2026-11-03 05:07:54',
              'elapsedDuration': 2460, 'activityType': {'typeKey': 'indoor_cycling'}}
RIDE_NOV3 = {'id': 'x', 'title': 'MyWhoosh - Mount Grizzly', 'date': 1793678915,
             'startDatetime': '2026-11-03T04:08:35.000Z', 'rideDuration': '00:39:11.000'}


def test_watch_recording_counts_as_duplicate_regardless_of_name():
    """Regression: 'Indoor Cycling' did not name-match the MyWhoosh title and was re-uploaded."""
    processor = ActivityProcessor(MagicMock(), MagicMock(), _garmin_with([WATCH_NOV3]))
    assert processor._is_on_garmin(RIDE_NOV3, RIDE_NOV3['date'], RIDE_NOV3['title'])


def test_fallback_duplicate_check_compares_in_utc():
    svc = _garmin_with([WATCH_NOV3])
    ride_start = ActivityProcessor(MagicMock(), MagicMock(), svc)._parse_activity_date(1793678915)
    assert ride_start.tzinfo is not None
    assert svc.check_duplicate_activity(ride_start, 'MyWhoosh - Mount Grizzly', threshold_hours=0.1)


def test_offline_merge_files_dry_run(tmp_path):
    """--merge --file: no MyWhoosh login, local files merged into the overlapping watch recording."""
    import os
    import shutil
    watch, m1, m2 = tmp_path / "watch.fit", tmp_path / "m1.fit", tmp_path / "m2.fit"
    _make_watch(watch, duration=200)
    _make_mywhoosh(m1, start_offset=5, duration=80, clock_skew=-1)
    _make_mywhoosh(m2, start_offset=100, duration=90, clock_skew=-1)

    start = datetime.fromtimestamp(T0, tz=timezone.utc)
    garmin = _garmin_with([{'activityId': 42, 'activityName': 'Indoor Cycling',
                            'startTimeGMT': start.strftime('%Y-%m-%d %H:%M:%S'),
                            'elapsedDuration': 200, 'activityType': {'typeKey': 'indoor_cycling'}}])
    def fake_download(activity_id, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return shutil.copy(watch, dest)
    garmin.download_original_fit = fake_download
    mywhoosh = MagicMock()
    processor = ActivityProcessor(mywhoosh, MagicMock(), garmin)

    stats = processor.process_merge_files([str(m2), str(m1)], dry_run=True,
                                          output_dir=str(tmp_path / "out"),
                                          backup_dir=str(tmp_path / "bak"))

    assert stats == {'total': 2, 'merged': 2, 'uploaded': 0, 'skipped': 0, 'errors': 0}
    mywhoosh.authenticate.assert_not_called()
    mywhoosh.download_activity.assert_not_called()
    merged = read_fit(str(tmp_path / "out" / "merged_42.fit")).messages(RECORD)
    gps = [i for i, r in enumerate(merged) if r.get(0) is not None]
    assert gps[0] == 5 and gps[-1] == 189   # both rides placed in the one recording
    assert m1.exists() and m2.exists()     # user's files are not deleted


# --- naming -----------------------------------------------------------------

from services.route_names import RouteNameCache  # noqa: E402


def test_route_name_cache_persists(tmp_path):
    path = str(tmp_path / "routes.json")
    RouteNameCache(path).set(90, "San Francisco Spin")
    assert RouteNameCache(path).get(90) == "San Francisco Spin"
    assert RouteNameCache(path).get(91) is None


def test_name_from_garmin_location():
    assert ActivityProcessor._name_from_garmin({'activityName': 'Mompós Virtual Cycling'}) == \
        'MyWhoosh - Mompós'
    assert ActivityProcessor._name_from_garmin({'activityName': ''}) is None


def _offline_processor(tmp_path, route_names):
    import os
    import shutil
    watch, ride = tmp_path / "watch.fit", tmp_path / "a.fit"
    _make_watch(watch, duration=200)
    _make_mywhoosh(ride, start_offset=5, duration=150, clock_skew=0)
    start = datetime.fromtimestamp(T0, tz=timezone.utc)
    garmin = _garmin_with([{'activityId': 42, 'activityName': 'Indoor Cycling',
                            'startTimeGMT': start.strftime('%Y-%m-%d %H:%M:%S'),
                            'elapsedDuration': 200, 'activityType': {'typeKey': 'indoor_cycling'}}])

    def fake_download(activity_id, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return shutil.copy(watch, dest)
    garmin.download_original_fit = fake_download
    garmin.replace_activity = MagicMock(return_value=43)
    processor = ActivityProcessor(MagicMock(), MagicMock(), garmin, route_names=route_names)
    processor.fit_merge_service.route_id = lambda path: 90
    return processor, str(ride), garmin


def _uploaded_name(garmin):
    return garmin.replace_activity.call_args.args[3]


def test_offline_name_from_route_cache(tmp_path):
    cache = RouteNameCache(str(tmp_path / "routes.json"))
    cache.set(90, "San Francisco Spin")
    processor, ride, garmin = _offline_processor(tmp_path, cache)
    processor.process_merge_files([ride], backup_dir=str(tmp_path / "bak"))
    assert _uploaded_name(garmin) == "MyWhoosh - San Francisco Spin"


def test_offline_name_option_gets_prefix(tmp_path):
    processor, ride, garmin = _offline_processor(tmp_path, RouteNameCache(str(tmp_path / "r.json")))
    processor.process_merge_files([ride], name="Evening Spin", backup_dir=str(tmp_path / "bak"))
    assert _uploaded_name(garmin) == "MyWhoosh - Evening Spin"
    assert processor.route_names.get(90) == "Evening Spin"  # remembered for this route


def test_offline_unknown_route_falls_back_to_garmin_place(tmp_path):
    processor, ride, garmin = _offline_processor(tmp_path, RouteNameCache(str(tmp_path / "r.json")))
    processor.process_merge_files([ride], backup_dir=str(tmp_path / "bak"))
    name = _uploaded_name(garmin)
    assert callable(name)
    assert name({'activityName': 'El Dorado County Virtual Cycling'}) == "MyWhoosh - El Dorado County"


def test_online_download_records_route_name(tmp_path):
    cache = RouteNameCache(str(tmp_path / "routes.json"))
    processor = ActivityProcessor(MagicMock(), MagicMock(), MagicMock(), route_names=cache)
    processor.mywhoosh_service.download_activity.return_value = "/tmp/x.fit"
    processor.fit_merge_service.route_id = lambda path: 85
    processor._ride_file({'path': None, 'activity': {}, 'title': 'MyWhoosh - Mount Grizzly'})
    assert cache.get(85) == "Mount Grizzly"
