#!/usr/bin/env python3
"""Watch iCloud directory for new Health Auto Export JSON files and process them."""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Timer

from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

# Load .env from the script's directory
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

api_key = os.getenv("AIRTABLE_API_KEY")
if not api_key:
    raise EnvironmentError("AIRTABLE_API_KEY not set. Check your .env file.")

WATCH_DIR = Path.home() / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/Daily-sleep"
LOG_PATH = Path.home() / "Library/Logs/sleep_airtable_watcher.log"
PROCESSED_STATE_PATH = Path.home() / "Library/Logs/sleep_airtable_watcher_state.json"
UPDATER_SCRIPT = SCRIPT_DIR / "sleep_airtable_updater.py"
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
                result = subprocess.run(
                    [sys.executable, str(UPDATER_SCRIPT), "--input", filepath],
                    capture_output=True,
                    text=True,
                    cwd=str(SCRIPT_DIR),
                    timeout=120,
                )

                if result.returncode == 0:
                    output = result.stdout.strip()
                    # Extract summary from last line
                    lines = output.split("\n")
                    summary = lines[-1] if lines else "completed"
                    logger.info(f"DONE {filename} → {summary}")
                    self._processed[filepath] = mtime
                    self._save_state()
                    return
                else:
                    error_msg = result.stderr.strip() or result.stdout.strip()
                    if attempt == 0:
                        logger.info(f"RETRY {filename} → {error_msg}")
                        time.sleep(10)
                        continue
                    logger.info(f"ERROR {filename} → {error_msg}")
                    return

            except subprocess.TimeoutExpired:
                logger.info(f"ERROR {filename} → timed out after 120s")
                return
            except Exception as e:
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
