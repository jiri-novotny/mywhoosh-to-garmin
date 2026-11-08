"""Activity processor for orchestrating the MyWhoosh to Garmin workflow."""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from services.mywhoosh_service import MyWhooshService
from services.fit_file_service import FitFileService
from services.fit_merge_service import FitMergeService
from services.garmin_service import GarminService
from services.route_names import RouteNameCache


class ActivityProcessor:
    """Main orchestrator for processing activities from MyWhoosh to Garmin."""

    def __init__(self,
                 mywhoosh_service: MyWhooshService,
                 fit_file_service: FitFileService,
                 garmin_service: GarminService,
                 fit_merge_service: Optional[FitMergeService] = None,
                 route_names: Optional[RouteNameCache] = None):
        """Initialize ActivityProcessor with injected services.

        Args:
            mywhoosh_service: Service for MyWhoosh operations
            fit_file_service: Service for FIT file operations
            garmin_service: Service for Garmin operations
        """
        self.mywhoosh_service = mywhoosh_service
        self.fit_file_service = fit_file_service
        self.garmin_service = garmin_service
        self.fit_merge_service = fit_merge_service or FitMergeService()
        self.route_names = route_names or RouteNameCache()
        self.logger = logging.getLogger(__name__)

    def process_latest_activity(self, check_duplicates: bool = True) -> bool:
        """Process the latest activity from MyWhoosh to Garmin.

        Args:
            check_duplicates: Whether to check for duplicate activities on Garmin

        Returns:
            True if successful, False otherwise
        """
        original_file_path: Optional[str] = None
        modified_file_path: Optional[str] = None

        try:
            # Header
            self.logger.info("=" * 70)
            self.logger.info("MyWhoosh to Garmin Connect Activity Sync")
            self.logger.info("=" * 70)

            # Step 1: Authenticate with MyWhoosh
            self.logger.info("Step 1: Authenticating with MyWhoosh...")
            self.mywhoosh_service.authenticate()

            # Step 2: Get latest activity
            self.logger.info("Step 2: Fetching latest activity...")
            latest_activity = self.mywhoosh_service.get_latest_activity()

            if not latest_activity:
                self.logger.info("No activities found - nothing to sync")
                return False

            # Extract activity metadata for logging and duplicate checking
            activity_id = latest_activity.get('id', latest_activity.get('_id', 'unknown'))
            activity_name = latest_activity.get('name', latest_activity.get('title', 'Unknown Activity'))
            activity_date_str = (
                latest_activity.get('date') or 
                latest_activity.get('startTime') or 
                latest_activity.get('createdAt') or 
                latest_activity.get('timestamp')
            )

            self.logger.info(f"Activity: {activity_name}")
            self.logger.info(f"Activity ID: {activity_id}")
            self.logger.info(f"Activity date: {activity_date_str}")

            # Step 3: Check for duplicates (if enabled)
            if check_duplicates:
                self.logger.info("Step 3: Checking for duplicates on Garmin Connect...")
                
                # Authenticate with Garmin early for duplicate check
                if not self.garmin_service.is_authenticated():
                    self.garmin_service.authenticate()

                if self._is_on_garmin(latest_activity, activity_date_str, activity_name):
                    self.logger.warning("⚠ Duplicate activity found - skipping upload")
                    self.logger.info("=" * 70)
                    self.logger.info("✓ Sync completed (duplicate skipped)")
                    self.logger.info("=" * 70)
                    return True  # Not an error - just skipped
            else:
                self.logger.info("Step 3: Duplicate check disabled - skipping")

            # Step 4: Download activity FIT file
            self.logger.info("Step 4: Downloading activity FIT file...")
            original_file_path = self.mywhoosh_service.download_activity(latest_activity)
            self.logger.info(f"Downloaded to: {original_file_path}")

            # Step 5: Modify FIT file device info
            self.logger.info("Step 5: Modifying FIT file device info...")
            modified_file_path = self.fit_file_service.modify_device_info(original_file_path)
            self.logger.info(f"Modified file: {modified_file_path}")

            # Step 6: Authenticate with Garmin (if not already done)
            if not self.garmin_service.is_authenticated():
                self.logger.info("Step 6: Authenticating with Garmin Connect...")
                self.garmin_service.authenticate()
            else:
                self.logger.info("Step 6: Already authenticated with Garmin Connect")

            # Step 7: Upload to Garmin Connect
            self.logger.info("Step 7: Uploading to Garmin Connect...")
            response = self.garmin_service.upload_activity(modified_file_path)

            # Success!
            self.logger.info("=" * 70)
            self.logger.info("✓ Sync completed successfully!")
            self.logger.info("=" * 70)
            self.logger.debug(f"Upload response: {response}")

            return True

        except Exception as e:
            self.logger.error("=" * 70)
            self.logger.error("✗ Sync failed!")
            self.logger.error("=" * 70)
            self.logger.exception(f"Error: {e}")
            return False

        finally:
            # Cleanup temporary files
            self.logger.info("Cleaning up temporary files...")
            if original_file_path:
                self.fit_file_service.cleanup_file(original_file_path)
            if modified_file_path:
                self.fit_file_service.cleanup_file(modified_file_path)
            self.logger.info("Cleanup complete")

    def _is_on_garmin(self, activity: Dict[str, Any], activity_date_str: Any, activity_name: str) -> bool:
        """True if the ride already exists on Garmin Connect.

        Any cycling activity covering the ride counts, whatever its name: a watch
        recording of the same ride ("Indoor Cycling") is a duplicate too.
        """
        window = self._mywhoosh_window(activity)
        if window is None:
            # No start/duration in the listing: fall back to the time-window check.
            activity_date = self._parse_activity_date(activity_date_str)
            if activity_date is None:
                self.logger.warning(f"Could not parse activity date: {activity_date_str}. "
                                    f"Continuing with upload...")
                return False
            return self.garmin_service.check_duplicate_activity(activity_date, activity_name)

        matches = self.garmin_service.find_overlapping_activities(*window)
        if not matches:
            self.logger.info("No overlapping activity on Garmin Connect")
            return False
        for match in matches:
            self.logger.info(f"Overlapping Garmin activity: '{match.get('activityName')}' "
                             f"({match.get('activityId')}, {match.get('startTimeGMT')} UTC)")
        if not any(self.garmin_service.is_virtual_ride(m) for m in matches):
            self.logger.info("Ride was recorded by the watch - use --merge to add the MyWhoosh route to it")
        return True

    def _parse_activity_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse activity date from various possible formats.

        Args:
            date_str: Date string in various formats (ISO, timestamp, etc.)

        Returns:
            datetime object or None if parsing fails
        """
        if not date_str:
            return None

        # Try parsing as Unix timestamp (numeric)
        try:
            timestamp = float(date_str)
            # Timestamps are typically in seconds, but could be milliseconds
            # If > 100 billion, likely milliseconds
            if timestamp > 100_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, AttributeError, OSError):
            pass

        # Try different date formats
        date_formats = [
            '%Y-%m-%dT%H:%M:%S.%fZ',      # ISO with milliseconds and Z
            '%Y-%m-%dT%H:%M:%SZ',          # ISO with Z
            '%Y-%m-%dT%H:%M:%S',           # ISO without Z
            '%Y-%m-%d %H:%M:%S',           # Space separated
            '%Y-%m-%d',                     # Date only
        ]

        for fmt in date_formats:
            try:
                return datetime.strptime(date_str.replace('+00:00', ''), fmt)
            except (ValueError, AttributeError):
                continue

        # Try isoformat parsing
        try:
            date_str_clean = date_str.replace('Z', '+00:00')
            return datetime.fromisoformat(date_str_clean)
        except (ValueError, AttributeError):
            pass

        self.logger.warning(f"Could not parse date: {date_str}")
        return None

    def process_multiple_activities(self, limit: int = 10, check_duplicates: bool = True) -> dict:
        """Process multiple activities from MyWhoosh to Garmin.

        Args:
            limit: Maximum number of activities to sync (default: 10)
            check_duplicates: Whether to check for duplicate activities on Garmin

        Returns:
            Dictionary with sync statistics:
                - 'total': Total activities processed
                - 'synced': Successfully synced activities
                - 'skipped': Skipped activities (duplicates or errors)
                - 'errors': Activities that failed to sync
        """
        stats = {
            'total': 0,
            'synced': 0,
            'skipped': 0,
            'errors': 0
        }

        try:
            # Header
            self.logger.info("=" * 70)
            self.logger.info("MyWhoosh to Garmin Connect - Batch Activity Sync")
            self.logger.info("=" * 70)

            # Step 1: Authenticate with services
            self.logger.info("Step 1: Authenticating with services...")
            self.mywhoosh_service.authenticate()
            if check_duplicates:
                self.garmin_service.authenticate()

            # Step 2: Get all activities
            self.logger.info(f"Step 2: Fetching activities (limit: {limit})...")
            activities = self.mywhoosh_service.get_activities(limit=limit)

            if not activities:
                self.logger.info("No activities found - nothing to sync")
                return stats

            # Apply limit to activities list
            activities = activities[:limit]
            self.logger.info(f"Found {len(activities)} activities to process")

            # Process each activity
            for i, activity in enumerate(activities, 1):
                try:
                    self.logger.info(f"\n--- Processing activity {i}/{len(activities)} ---")
                    stats['total'] += 1

                    # Extract activity metadata
                    activity_id = activity.get('id', activity.get('_id', 'unknown'))
                    activity_name = activity.get('name', activity.get('title', 'Unknown Activity'))
                    activity_date_str = (
                        activity.get('date') or
                        activity.get('startTime') or
                        activity.get('createdAt') or
                        activity.get('timestamp')
                    )

                    self.logger.info(f"Activity: {activity_name} (ID: {activity_id})")

                    # Check for duplicates if enabled
                    if check_duplicates and self._is_on_garmin(activity, activity_date_str, activity_name):
                        self.logger.warning("⚠ Duplicate activity - skipping")
                        stats['skipped'] += 1
                        continue

                    # Download and sync activity
                    original_file_path = None
                    modified_file_path = None

                    try:
                        # Download
                        original_file_path = self.mywhoosh_service.download_activity(activity)
                        self.logger.info("Downloaded FIT file")

                        # Modify
                        modified_file_path = self.fit_file_service.modify_device_info(original_file_path)
                        self.logger.info("Modified FIT file")

                        # Upload
                        if not self.garmin_service.is_authenticated():
                            self.garmin_service.authenticate()

                        self.garmin_service.upload_activity(modified_file_path)
                        self.logger.info("✓ Activity synced successfully")
                        stats['synced'] += 1

                    except Exception as e:
                        self.logger.exception(f"✗ Failed to sync activity: {e}")
                        stats['errors'] += 1

                    finally:
                        # Cleanup temp files
                        if original_file_path:
                            self.fit_file_service.cleanup_file(original_file_path)
                        if modified_file_path:
                            self.fit_file_service.cleanup_file(modified_file_path)

                except Exception as e:
                    self.logger.exception(f"Error processing activity: {e}")
                    stats['errors'] += 1
                    continue

            # Summary
            self.logger.info("\n" + "=" * 70)
            self.logger.info("Batch Sync Summary")
            self.logger.info("=" * 70)
            self.logger.info(f"Total processed: {stats['total']}")
            self.logger.info(f"Synced: {stats['synced']}")
            self.logger.info(f"Skipped: {stats['skipped']}")
            self.logger.info(f"Errors: {stats['errors']}")
            self.logger.info("=" * 70)

            return stats

        except Exception as e:
            self.logger.error("=" * 70)
            self.logger.error("✗ Batch sync failed!")
            self.logger.error("=" * 70)
            self.logger.exception(f"Error: {e}")
            return stats


    # ------------------------------------------------------------------
    # Merge mode: MyWhoosh route + watch recording -> one Garmin activity
    # ------------------------------------------------------------------

    MYWHOOSH_TITLE_PREFIX = "MyWhoosh - "
    # Extra MyWhoosh rides fetched so that every ride inside a watch recording is
    # merged, even if some of them fall outside the requested limit.
    LOOKAHEAD = 10

    @staticmethod
    def _mywhoosh_window(activity: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
        """(start_utc, end_utc) of a MyWhoosh listing entry, from startDatetime/date + rideDuration."""
        start = None
        if activity.get('startDatetime'):
            start = datetime.fromisoformat(activity['startDatetime'].replace('Z', '+00:00'))
        elif isinstance(activity.get('date'), (int, float)):
            start = datetime.fromtimestamp(activity['date'], tz=timezone.utc)
        duration = activity.get('rideDuration')
        if start is None or not duration:
            return None
        h, m, sec = duration.split(':')
        return start, start + timedelta(hours=int(h), minutes=int(m), seconds=float(sec))

    @classmethod
    def _merged_name(cls, rides: List[Dict[str, Any]]) -> str:
        titles = []
        for ride in rides:
            title = ride.get('title') or ride.get('name') or ride.get('routeName') or 'Ride'
            if title.startswith(cls.MYWHOOSH_TITLE_PREFIX):
                title = title[len(cls.MYWHOOSH_TITLE_PREFIX):]
            titles.append(title)
        return cls.MYWHOOSH_TITLE_PREFIX + " + ".join(titles)

    @classmethod
    def _strip_prefix(cls, title: Optional[str]) -> Optional[str]:
        if title and title.startswith(cls.MYWHOOSH_TITLE_PREFIX):
            title = title[len(cls.MYWHOOSH_TITLE_PREFIX):]
        return title or None

    @classmethod
    def _with_prefix(cls, title: str) -> str:
        return title if title.startswith(cls.MYWHOOSH_TITLE_PREFIX) else cls.MYWHOOSH_TITLE_PREFIX + title

    VIRTUAL_NAME_SUFFIXES = (" Virtual Cycling", " Virtual Ride", " Cycling")

    @classmethod
    def _name_from_garmin(cls, activity: Dict[str, Any]) -> Optional[str]:
        """'Mompós Virtual Cycling' (Garmin's location-based name) -> 'MyWhoosh - Mompós'."""
        place = (activity.get('activityName') or '').strip()
        for suffix in cls.VIRTUAL_NAME_SUFFIXES:
            if place.endswith(suffix):
                place = place[:-len(suffix)].strip()
                break
        return cls.MYWHOOSH_TITLE_PREFIX + place if place else None

    @staticmethod
    def _is_mywhoosh_file(path: str) -> bool:
        """MyWhoosh FIT files carry a 'mywhoosh.whoosh...' developer application id."""
        with open(path, 'rb') as f:
            return b'mywhoosh' in f.read()

    def process_merge(self, limit: int = 1, dry_run: bool = False,
                      output_dir: str = 'merged', backup_dir: str = 'backups') -> dict:
        """Merge the latest MyWhoosh rides into the matching watch recordings on Garmin.

        For every MyWhoosh ride, the overlapping cycling activity recorded by the
        watch is downloaded, the MyWhoosh route/distance is merged into it and the
        watch activity is replaced by the merged one. Rides with no watch recording
        are uploaded as before (MyWhoosh file with Garmin device info).

        Args:
            limit: Number of latest MyWhoosh rides to process
            dry_run: Only write merged files to `output_dir`; don't upload/delete anything
            output_dir: Where merged files are written in dry-run mode
            backup_dir: Where original watch FIT files are kept before replacing

        Returns:
            Stats dict: total, merged, uploaded, skipped, errors
        """
        self._log_merge_header(dry_run)
        self.mywhoosh_service.authenticate()
        self.garmin_service.authenticate()

        listing = self.mywhoosh_service.get_activities(limit=limit + self.LOOKAHEAD)
        candidates = [{'id': a['id'], 'title': a.get('title') or a.get('name'),
                       'window': self._mywhoosh_window(a), 'activity': a, 'path': None}
                      for a in listing]
        return self._merge_rides(candidates[:limit], candidates, dry_run, output_dir, backup_dir)

    def process_merge_files(self, paths: List[str], name: Optional[str] = None, dry_run: bool = False,
                            output_dir: str = 'merged', backup_dir: str = 'backups') -> dict:
        """Offline variant of process_merge: MyWhoosh FIT files given locally, no MyWhoosh login.

        Args:
            paths: MyWhoosh FIT files (several rides inside one watch recording are merged together)
            name: Activity name on Garmin ("MyWhoosh - " is prepended if missing). Default:
                "MyWhoosh - <route>" if the route was seen online before (route cache),
                otherwise "MyWhoosh - <place>" from the name Garmin assigns.
        """
        self._log_merge_header(dry_run)
        self.garmin_service.authenticate()

        rides = []
        stats_errors = 0
        for path in paths:
            try:
                window = self.fit_merge_service.time_window(path)
            except Exception as e:
                self.logger.error(f"Cannot read MyWhoosh file {path}: {e}")
                stats_errors += 1
                continue
            route_id = self.fit_merge_service.route_id(path)
            title = self.route_names.get(route_id)
            self.logger.info(f"File {path}: {window[0]:%Y-%m-%d %H:%M:%S} - {window[1]:%H:%M:%S} UTC, "
                             f"route {route_id}: {title or 'name unknown (not ridden online yet)'}")
            rides.append({'id': os.path.abspath(path), 'title': title, 'window': window,
                          'activity': None, 'path': path})
        if name:
            name = self._with_prefix(name)
            if len(rides) == 1:  # remember it, so the next file of this route is named automatically
                self.route_names.set(self.fit_merge_service.route_id(rides[0]['path']),
                                     self._strip_prefix(name))
        stats = self._merge_rides(rides, rides, dry_run, output_dir, backup_dir, name=name)
        stats['total'] += stats_errors
        stats['errors'] += stats_errors
        return stats

    def _log_merge_header(self, dry_run: bool) -> None:
        self.logger.info("=" * 70)
        self.logger.info(f"MyWhoosh + watch merge{' (dry run)' if dry_run else ''}")
        self.logger.info("=" * 70)

    def _merge_rides(self, rides: List[Dict[str, Any]], candidates: List[Dict[str, Any]],
                     dry_run: bool, output_dir: str, backup_dir: str,
                     name: Optional[str] = None) -> dict:
        """Group rides by the watch recording that covers them and merge each group.

        `rides` are the rides to process; `candidates` (a superset) are used to pull
        in every ride that lies inside the same watch recording.
        """
        stats = {'total': len(rides), 'merged': 0, 'uploaded': 0, 'skipped': 0, 'errors': 0}
        if not rides:
            self.logger.info("No MyWhoosh activities to process")
            return stats

        groups: Dict[int, Dict[str, Any]] = {}
        unmatched = []
        for ride in rides:
            if ride['window'] is None:
                self.logger.error(f"Cannot determine time window of MyWhoosh ride {ride['id']}")
                stats['errors'] += 1
                continue
            matches = self.garmin_service.find_overlapping_activities(*ride['window'])
            watch = [m for m in matches if not self.garmin_service.is_virtual_ride(m)]
            virtual = [m for m in matches if self.garmin_service.is_virtual_ride(m)]
            if watch:
                group = groups.setdefault(int(watch[0]['activityId']),
                                          {'garmin': watch[0], 'stale': {}})
                # Virtual rides next to a watch recording are leftovers of a plain
                # MyWhoosh upload; they are removed once the merge succeeded.
                for v in virtual:
                    group['stale'][int(v['activityId'])] = v
            elif virtual:
                self.logger.info(f"'{ride['title'] or ride['id']}': already on Garmin as a virtual ride - skipping")
                stats['skipped'] += 1
            else:
                unmatched.append(ride)

        requested_ids = {r['id'] for r in rides}
        for gid, group in groups.items():
            g_start, g_end = self.garmin_service.activity_window(group['garmin'])
            # Each group gets *all* candidate rides inside the watch recording.
            members = sorted((c for c in candidates if c['window']
                              and c['window'][0] < g_end and c['window'][1] > g_start),
                             key=lambda c: c['window'][0])
            requested = sum(1 for m in members if m['id'] in requested_ids)
            try:
                self._merge_group(gid, group['garmin'], members, dry_run, output_dir, backup_dir,
                                  stale=list(group['stale'].values()), name=name)
                stats['merged'] += requested
            except Exception as e:
                self.logger.exception(f"✗ Failed to merge into Garmin activity {gid}: {e}")
                stats['errors'] += requested

        for ride in unmatched:
            self.logger.info(f"'{ride['title'] or ride['id']}': no watch recording found - uploading MyWhoosh file")
            if dry_run:
                stats['skipped'] += 1
            elif self._upload_mywhoosh_ride(ride):
                stats['uploaded'] += 1
            else:
                stats['errors'] += 1

        self.logger.info("=" * 70)
        self.logger.info(f"Merge summary: {stats}")
        self.logger.info("=" * 70)
        return stats

    def _ride_file(self, ride: Dict[str, Any]) -> Tuple[str, bool]:
        """Return (path, is_temporary) of the ride's MyWhoosh FIT file."""
        if ride['path']:
            return ride['path'], False
        path = self.mywhoosh_service.download_activity(ride['activity'])
        try:  # remember the route name for offline files (their Title field is corrupted)
            self.route_names.set(self.fit_merge_service.route_id(path), self._strip_prefix(ride['title']))
        except Exception as e:
            self.logger.debug(f"Could not record route name: {e}")
        return path, True

    def _merge_group(self, garmin_id: int, garmin: Dict[str, Any], rides: List[Dict[str, Any]],
                     dry_run: bool, output_dir: str, backup_dir: str,
                     stale: Optional[List[Dict[str, Any]]] = None,
                     name: Optional[str] = None) -> None:
        titles = [r['title'] for r in rides if r['title']]
        if name is None and titles:
            name = self._merged_name([{'title': t} for t in titles])
        self.logger.info(f"Garmin activity {garmin_id} '{garmin.get('activityName')}' "
                         f"<- {len(rides)} MyWhoosh ride(s): {name or 'MyWhoosh - <place named by Garmin>'}")
        if name is None:
            name = self._name_from_garmin

        backup_path = os.path.join(backup_dir, f"garmin_{garmin_id}.fit")
        self.garmin_service.download_original_fit(garmin_id, backup_path)
        if self._is_mywhoosh_file(backup_path):
            raise RuntimeError(f"Garmin activity {garmin_id} is a MyWhoosh upload, not a watch recording")

        # Only delete leftovers that really are MyWhoosh files (kept in backups too).
        stale_ids = []
        for activity in stale or []:
            sid = int(activity['activityId'])
            path = self.garmin_service.download_original_fit(sid, os.path.join(backup_dir, f"garmin_{sid}.fit"))
            if not self._is_mywhoosh_file(path):
                raise RuntimeError(f"Garmin activity {sid} '{activity.get('activityName')}' overlaps the "
                                   f"watch recording but is not a MyWhoosh upload - resolve manually")
            stale_ids.append(sid)

        temp_paths = []
        mywhoosh_paths = []
        merged_path = None
        try:
            for ride in rides:
                path, is_temp = self._ride_file(ride)
                mywhoosh_paths.append(path)
                if is_temp:
                    temp_paths.append(path)
            if dry_run:
                os.makedirs(output_dir, exist_ok=True)
                merged_path = os.path.join(output_dir, f"merged_{garmin_id}.fit")
            merged_path = self.fit_merge_service.merge(backup_path, mywhoosh_paths, merged_path)

            if dry_run:
                self.logger.info(f"Dry run: merged file written to {merged_path}")
                for sid in stale_ids:
                    self.logger.info(f"Dry run: would delete duplicate MyWhoosh upload {sid}")
                return
            new_id = self.garmin_service.replace_activity(
                garmin, merged_path, backup_path, name, exclude_ids=stale_ids)
            self.logger.info(f"✓ Replaced Garmin activity {garmin_id} with merged activity {new_id}")
            for sid in stale_ids:
                self.garmin_service.client.delete_activity(sid)
                self.logger.info(f"Deleted duplicate MyWhoosh upload {sid} (backup in {backup_dir})")
        finally:
            for path in temp_paths:
                self.fit_file_service.cleanup_file(path)
            if merged_path and not dry_run:
                self.fit_file_service.cleanup_file(merged_path)

    def _upload_mywhoosh_ride(self, ride: Dict[str, Any]) -> bool:
        """Previous behaviour: upload the MyWhoosh file with Garmin device info."""
        original = modified = None
        is_temp = False
        label = ride['title'] or ride['id']
        try:
            original, is_temp = self._ride_file(ride)
            modified = self.fit_file_service.modify_device_info(original)
            self.garmin_service.upload_activity(modified)
            self.logger.info(f"✓ Uploaded '{label}'")
            return True
        except Exception as e:
            self.logger.exception(f"✗ Failed to upload '{label}': {e}")
            return False
        finally:
            if original and is_temp:
                self.fit_file_service.cleanup_file(original)
            if modified:
                self.fit_file_service.cleanup_file(modified)
