# Sleep Data → Airtable Updater

This system automatically takes sleep data exported by the Health Auto Export iOS app, parses it, and uploads it to an Airtable base. It consists of two Python scripts: one that parses a JSON file and inserts records into Airtable (`sleep_airtable_updater.py`), and one that watches the iCloud folder for new exports and triggers the updater automatically (`sleep_watcher.py`).

## Prerequisites

- Python 3.8+
- An Airtable account with access to the Health-Tracking base
- Health Auto Export app (healthyapps.dev) configured to export daily sleep data to the iCloud folder

## Setup

1. The scripts live at `/Users/rick/scripts/sleep-airtable/`.

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Copy the example environment file and fill in your API key:
   ```
   cp .env.example .env
   ```

4. Edit `.env` and replace the placeholder with your real Airtable API key. To get one: go to Airtable → Account → Developer Hub → Personal access tokens. Create a token with read/write access to the Health-Tracking base.

5. Make sure the watch directory exists:
   ```
   ls ~/Library/Mobile\ Documents/iCloud~com~ifunography~HealthExport/Documents/Daily-sleep
   ```
   If it doesn't exist, open Health Auto Export on your phone and configure it to export to this iCloud folder.

## Usage: Manual Run

Insert new nights from a specific file:
```
python sleep_airtable_updater.py --input ~/Downloads/HealthAutoExport-2026-03-22.json
```

Preview what would be inserted without writing:
```
python sleep_airtable_updater.py --input ~/Downloads/HealthAutoExport-2026-03-22.json --dry-run
```

Process only a specific date range:
```
python sleep_airtable_updater.py --input ~/Downloads/HealthAutoExport-2026-03-22.json --start-date 2026-03-01 --end-date 2026-03-22
```

## Usage: Automatic Mode (File Watcher)

Start it manually:
```
python sleep_watcher.py
```

Install as a background service that starts on login:
```
cp com.rick.sleep_watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rick.sleep_watcher.plist
```

Check if it's running:
```
launchctl list | grep sleep_watcher
```

View logs:
```
tail -f ~/Library/Logs/sleep_airtable_watcher.log
```

Stop it:
```
launchctl unload ~/Library/LaunchAgents/com.rick.sleep_watcher.plist
```

## How It Works

- Health Auto Export exports sleep data nightly to the iCloud folder as JSON.
- The watcher detects the new file and triggers the updater after a 5-second debounce.
- The updater parses the JSON, resolves overlapping sleep stage intervals from multiple sources, and inserts one record per night per source into Airtable.
- Nights already in Airtable (matched by date + source) are skipped automatically — no duplicates.

## Airtable Target

- Base: Health-Tracking (`appBmQA2p3z2Fdofa`)
- Table: Sleep (`tbl9nIjgF1dE079YK`)
- One row per night per source

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `AIRTABLE_API_KEY not set` | `.env` missing or misnamed | Copy `.env.example` to `.env` and fill it in |
| All nights skipped | Records already exist | Normal — use `--dry-run` to verify |
| JSON parse error | iCloud sync incomplete | Wait 30 seconds and re-run manually |
| Watcher not triggering | launchd not loaded | Run `launchctl list \| grep sleep_watcher` |
