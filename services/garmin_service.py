"""Garmin service for handling authentication and activity uploads."""

import io
import logging
import os
import sys
import time
import zipfile
from typing import Callable, Dict, Any, Iterable, List, Optional, Union
from datetime import datetime, timedelta, timezone
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError
)
from garth.exc import GarthHTTPError


class GarminService:
    """Service for interacting with Garmin Connect."""

    DEFAULT_TOKENSTORE = "~/.garminconnect"

    def __init__(self, username: str, password: str, tokenstore: Optional[str] = None):
        """Initialize GarminService with credentials.

        Args:
            username: Garmin Connect username
            password: Garmin Connect password
            tokenstore: Directory where OAuth tokens are cached between runs, so
                MFA is only needed once (defaults to $GARMINTOKENS or ~/.garminconnect)
        """
        self.username = username
        self.password = password
        self.tokenstore = os.path.expanduser(
            tokenstore or os.getenv("GARMINTOKENS") or self.DEFAULT_TOKENSTORE
        )
        self.client: Garmin = Garmin(username, password, prompt_mfa=self._prompt_mfa)
        self.logger = logging.getLogger(__name__)
        self._authenticated = False

    @staticmethod
    def _prompt_mfa() -> str:
        """Ask for the MFA code Garmin sent; fail clearly when not interactive."""
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Garmin account requires MFA. Run once interactively to enter the "
                "code; the tokens are then cached and reused."
            )
        return input("Garmin MFA code: ").strip()

    def _login_with_tokens(self) -> bool:
        """Try to log in from cached tokens. Returns True on success."""
        if not os.path.isdir(self.tokenstore):
            return False
        try:
            self.client.login(self.tokenstore)
            self.logger.info(f"Logged in to Garmin Connect using cached tokens ({self.tokenstore})")
            return True
        except Exception as e:
            self.logger.warning(f"Cached Garmin tokens not usable ({e}); logging in with credentials")
            # Start from a clean client so stale tokens don't interfere.
            self.client = Garmin(self.username, self.password, prompt_mfa=self._prompt_mfa)
            return False

    def _save_tokens(self) -> None:
        try:
            os.makedirs(self.tokenstore, mode=0o700, exist_ok=True)
            self.client.garth.dump(self.tokenstore)
            self.logger.info(f"Saved Garmin tokens to {self.tokenstore}")
        except Exception as e:
            self.logger.warning(f"Could not save Garmin tokens to {self.tokenstore}: {e}")

    def authenticate(self) -> None:
        """Authenticate with Garmin Connect.

        Raises:
            GarminConnectAuthenticationError: Invalid credentials
            GarminConnectTooManyRequestsError: Rate limit exceeded
            GarminConnectConnectionError: Network connection issues
            RuntimeError: Other authentication failures
        """
        self.logger.info("Logging in to Garmin Connect...")

        try:
            if not self._login_with_tokens():
                self.client.login()
                self._save_tokens()
            self._authenticated = True
            self.logger.info("Successfully authenticated with Garmin Connect")
        except GarminConnectAuthenticationError:
            self.logger.exception("Authentication error. Check your credentials.")
            raise
        except GarminConnectTooManyRequestsError:
            self.logger.exception("Too many requests. Try again later.")
            raise
        except GarminConnectConnectionError:
            self.logger.exception("Connection error. Check your internet connection.")
            raise
        except Exception as e:
            self.logger.exception(f"Failed to login to Garmin Connect: {e}")
            raise RuntimeError(f"Authentication failed: {e}") from e

    def upload_activity(self, fit_file_path: str) -> Any:
        """Upload a .fit file to Garmin Connect.

        Args:
            fit_file_path: Path to the FIT file to upload

        Returns:
            Upload response from Garmin Connect or success dict if 409 Conflict (duplicate)

        Raises:
            RuntimeError: If not authenticated or upload fails (other than 409)
        """
        if not self._authenticated:
            raise RuntimeError("Must authenticate before uploading activities")

        self.logger.info(f"Uploading {fit_file_path} to Garmin Connect...")

        try:
            response = self.client.upload_activity(fit_file_path)
            self.logger.info("Upload successful")
            self.logger.debug(f"Upload response: {response}")
            return response
        except Exception as e:
            error_str = str(e)
            
            # Handle 409 Conflict gracefully (activity already exists)
            if "409" in error_str or "Conflict" in error_str:
                self.logger.info("⚠ Activity already exists on Garmin Connect (409 Conflict)")
                self.logger.info("Treating as success - no duplicate upload needed")
                return {"status": "skipped", "message": "Activity already exists on Garmin Connect"}
            
            self.logger.exception(f"Failed to upload activity: {e}")
            raise RuntimeError(f"Upload failed: {e}") from e

    # ------------------------------------------------------------------
    # Activity lookup / download / replace (used by merge mode)
    # ------------------------------------------------------------------

    CYCLING_TYPE_HINTS = ("cycl", "bik", "ride")

    @staticmethod
    def activity_window(activity: Dict[str, Any]) -> Optional[tuple]:
        """Return (start_utc, end_utc) of a Garmin activity list entry."""
        start_str = activity.get("startTimeGMT")
        if not start_str:
            return None
        start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        duration = float(activity.get("elapsedDuration") or activity.get("duration") or 0)
        return start, start + timedelta(seconds=duration)

    @classmethod
    def is_cycling(cls, activity: Dict[str, Any]) -> bool:
        type_key = (activity.get("activityType") or {}).get("typeKey", "")
        return any(hint in type_key for hint in cls.CYCLING_TYPE_HINTS)

    @staticmethod
    def is_virtual_ride(activity: Dict[str, Any]) -> bool:
        return (activity.get("activityType") or {}).get("typeKey") == "virtual_ride"

    def find_overlapping_activities(self, start: datetime, end: datetime,
                                    tolerance_s: int = 600,
                                    exclude_ids: Iterable[int] = ()) -> List[Dict[str, Any]]:
        """Find cycling activities covering [start, end] (all but `tolerance_s` of it).

        Matching is by time overlap, not start time, because one watch recording
        can contain several MyWhoosh rides. Results are sorted by overlap, largest first.
        """
        if not self._authenticated:
            raise RuntimeError("Must authenticate before searching activities")
        exclude = {int(i) for i in exclude_ids}
        needed = (end - start).total_seconds() - tolerance_s
        # List dates are local; query a day either side of the UTC start.
        day = start.date()
        activities = self.client.get_activities_by_date(
            (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()) or []
        scored = []
        for activity in activities:
            if int(activity.get("activityId", 0)) in exclude or not self.is_cycling(activity):
                continue
            window = self.activity_window(activity)
            if window is None:
                continue
            overlap = (min(end, window[1]) - max(start, window[0])).total_seconds()
            if overlap >= needed:
                scored.append((overlap, activity))
        scored.sort(key=lambda s: -s[0])
        return [a for _, a in scored]

    def download_original_fit(self, activity_id: int, dest_path: str) -> str:
        """Download the original FIT file an activity was created from."""
        if not self._authenticated:
            raise RuntimeError("Must authenticate before downloading activities")
        data = self.client.download_activity(
            activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fit_names:
                raise RuntimeError(f"No FIT file in original download of {activity_id}: {zf.namelist()}")
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(zf.read(fit_names[0]))
        self.logger.info(f"Downloaded original FIT of Garmin activity {activity_id} -> {dest_path}")
        return dest_path

    def _wait_for_new_activity(self, start: datetime, end: datetime, exclude_ids: Iterable[int],
                               timeout_s: int = 90, interval_s: int = 5) -> Dict[str, Any]:
        """Poll until an uploaded activity covering [start, end] shows up (uploads are async)."""
        deadline = time.monotonic() + timeout_s
        while True:
            found = self.find_overlapping_activities(start, end, exclude_ids=exclude_ids)
            if found:
                return found[0]
            if time.monotonic() >= deadline:
                raise RuntimeError("Uploaded activity did not appear on Garmin Connect in time")
            time.sleep(interval_s)

    def replace_activity(self, original: Dict[str, Any], new_fit_path: str,
                         backup_fit_path: str,
                         name: Union[str, Callable[[Dict[str, Any]], Optional[str]], None] = None,
                         exclude_ids: Iterable[int] = ()) -> int:
        """Replace a Garmin activity with a new FIT file. Returns the new activity id.

        Order of operations keeps the original as long as possible:
        1. upload the new file; if Garmin rejects it as a duplicate of the original
           (409), delete the original first and upload again;
        2. wait until the new activity is visible, then delete the original;
        3. set the activity name.
        If the upload after deleting fails, the backup of the original is re-uploaded.
        `exclude_ids` are other activities in the same time window that must not be
        mistaken for the new upload.
        """
        original_id = int(original["activityId"])
        start, end = self.activity_window(original)
        deleted = False

        result = self.upload_activity(new_fit_path)
        if isinstance(result, dict) and result.get("status") == "skipped":
            self.logger.info("Garmin rejected the merged file as duplicate; deleting original first")
            self.client.delete_activity(original_id)
            deleted = True
            try:
                result = self.upload_activity(new_fit_path)
                if isinstance(result, dict) and result.get("status") == "skipped":
                    raise RuntimeError("Merged file still rejected as duplicate after deleting original")
            except Exception:
                self.logger.error(f"Upload failed after deleting original; restoring from {backup_fit_path}")
                self.upload_activity(backup_fit_path)
                raise

        new_activity = self._wait_for_new_activity(start, end,
                                                   exclude_ids=[original_id, *exclude_ids])
        new_id = int(new_activity["activityId"])
        self.logger.info(f"Merged activity uploaded as {new_id}")

        if not deleted:
            self.client.delete_activity(original_id)
            self.logger.info(f"Deleted original activity {original_id} (backup: {backup_fit_path})")

        if callable(name):  # name derived from the new activity (e.g. Garmin's location name)
            name = name(new_activity)
        if name:
            try:
                self.client.set_activity_name(new_id, name)
                self.logger.info(f"Renamed activity {new_id} to '{name}'")
            except Exception as e:
                self.logger.warning(f"Could not rename activity {new_id}: {e}")
        return new_id

    def is_authenticated(self) -> bool:
        """Check if the service is authenticated.

        Returns:
            True if authenticated, False otherwise
        """
        return self._authenticated

    def check_duplicate_activity(
        self,
        activity_date: datetime,
        activity_name: Optional[str] = None,
        threshold_hours: int = 2
    ) -> bool:
        """Check if a cycling activity starts within threshold_hours of activity_date.

        Fallback used when the ride's duration is unknown (otherwise see
        find_overlapping_activities). Times are compared in UTC; naive datetimes
        are treated as UTC. The name is only logged: a watch recording of the same
        ride is a duplicate even though it is called e.g. "Indoor Cycling".

        Raises:
            RuntimeError: If not authenticated
        """
        if not self._authenticated:
            raise RuntimeError("Must authenticate before checking activities")

        if activity_date.tzinfo is None:
            activity_date = activity_date.replace(tzinfo=timezone.utc)
        self.logger.info(
            f"Checking for duplicate of '{activity_name}' around "
            f"{activity_date:%Y-%m-%d %H:%M:%S} UTC (±{threshold_hours}h window)"
        )
        try:
            day = activity_date.date()
            activities = self.client.get_activities_by_date(
                (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()) or []
            for activity in activities:
                window = self.activity_window(activity)
                if window is None or not self.is_cycling(activity):
                    continue
                diff_h = abs((window[0] - activity_date).total_seconds()) / 3600
                if diff_h <= threshold_hours:
                    self.logger.info(f"Found duplicate: '{activity.get('activityName')}' "
                                     f"at {window[0]:%Y-%m-%d %H:%M:%S} UTC (Δ{diff_h:.1f}h)")
                    return True
            self.logger.info("No duplicate found")
            return False
        except Exception as e:
            self.logger.warning(f"Error checking for duplicates: {e}")
            # Don't block upload if duplicate check fails
            return False
