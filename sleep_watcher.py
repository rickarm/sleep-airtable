#!/usr/bin/env python3
"""Watch iCloud directory for new Health Auto Export JSON files and process them."""

import json
import logging
import os
import sys
import time
from pathlib import Path
from threading import Timer

from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

# Load .env from the script's directory before importing the updater, so that
# AIRTABLE_API_KEY is present when process_file() makes its first API call.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

# Import process_file directly rather than spawning a subprocess.
#
# Previously the watcher called:
#   subprocess.run([sys.executable, "sleep_airtable_updater.py", "--input", ...])
#
# That approach breaks whenever Homebrew upgrades Python: the venv symlinks
# immediately resolve to the new Cellar binary, which hasn't been granted Full
# Disk Access in macOS TCC. The long-running watcher process itself keeps its
# FDA (the binary image stays in memory), but every newly-spawned child process
# executes the upgraded binary — and gets a PermissionError on the iCloud path.
#
# Calling process_file() in-process eliminates the subprocess entirely: only
# the single watcher process ever needs FDA, and a Homebrew Python upgrade no
# longer breaks anything until the next time launchd restarts the service.
from sleep_airtable_updater import process_file  # noqa: E402

api_key = os.getenv("AIRTABLE_API_KEY")
if not api_key:
    raise EnvironmentError("AIRTABLE_API_KEY not set. Check your .env file.")

WATCH_DIR = Path.home() / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/Daily-sleep"
LOG_PATH = Path.home() / "Library/Logs/sleep_airtable_watcher.log"
PROCESSED_STATE_PATH = Path.home() / "Library/Logs/sleep_airtable_watcher_state.json"
DEBOUNCE_SECONDS = 5

# Set up logging
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class SleepFileHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._processed = self._load_state()  # filepath -> mtime, persisted across restarts
        self._pending_timers = {}  # filepath -> Timer

    def _load_state(self):
        """Load previously-processed file mtimes from disk."""
        try:
            if PROCESSED_STATE_PATH.exists():
                with open(PROCESSED_STATE_PATH) as f:
                    state = json.load(f)
                logger.info(f"Loaded processed state: {len(state)} files")
                return state
        except Exception as e:
            logger.warning(f"Could not load state file, starting fresh: {e}")
        return {}

    def _save_state(self):
        """Persist processed file mtimes to disk."""
        try:
            with open(PROCESSED_STATE_PATH, "w") as f:
                json.dump(self._processed, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state file: {e}")

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            self._handle(event.src_path)

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            self._handle(event.src_path)

    def _handle(self, filepath):
        if not filepath.endswith(".json"):
            return

        filename = os.path.basename(filepath)
        logger.info(f"DETECTED {filename}")

        # Cancel any existing timer for this file
        if filepath in self._pending_timers:
            self._pending_timers[filepath].cancel()

        # Debounce: wait before processing
        timer = Timer(DEBOUNCE_SECONDS, self._process, args=[filepath])
        self._pending_timers[filepath] = timer
        timer.start()

    def _process(self, filepath):
        self._pending_timers.pop(filepath, None)
        filename = os.path.basename(filepath)

        # Check mtime to avoid reprocessing
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            logger.info(f"ERROR {filename} → file not accessible")
            return

        if filepath in self._processed and self._processed[filepath] == mtime:
            logger.info(f"SKIPPED {filename} (already processed this version)")
            return

        logger.info(f"PROCESSING {filename}")

        # Try processing, retry once on failure (handles partial iCloud sync)
        for attempt in range(2):
            try:
                inserted, skipped, errors = process_file(filepath)
                summary = f"Done. Processed 1 nights: {inserted} inserted, {skipped} skipped, {errors} errors."
                logger.info(f"DONE {filename} → {summary}")
                self._processed[filepath] = mtime
                self._save_state()
                return
            except Exception as e:
                if attempt == 0:
                    logger.info(f"RETRY {filename} → {e}")
                    time.sleep(10)
                    continue
                logger.info(f"ERROR {filename} → {e}")
                return


def main():
    if not WATCH_DIR.exists():
        logger.error(f"Watch directory does not exist: {WATCH_DIR}")
        logger.error("Make sure Health Auto Export is configured to export to this folder.")
        sys.exit(1)

    logger.info(f"Starting sleep watcher on {WATCH_DIR}")

    handler = SleepFileHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping sleep watcher")
        observer.stop()

    observer.join()


if __name__ == "__main__":
    main()
