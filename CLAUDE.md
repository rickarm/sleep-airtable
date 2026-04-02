# Sleep-Airtable: Health Sleep Data Sync

Watches for Apple Health sleep exports (via Health Auto Export iOS app) and syncs to Airtable. Runs as a launchd watcher daemon.

## Development Workflow

See `KB-Development-Workflow.md` in the Knowledge Base for the full workflow. Summary:

1. Bugs and features are tracked as **GitHub Issues**
2. Claude works on a **feature branch** (worktrees for isolation in local sessions)
3. Claude pushes the branch and opens a **Pull Request**
4. Rick reviews and merges the PR
5. Adding the `claude` label to an issue triggers Claude via GitHub Actions

## Commands

```bash
# Manual import
python sleep_airtable_updater.py --input ~/Downloads/HealthAutoExport-2026-03-22.json

# Dry run (no writes)
python sleep_airtable_updater.py --input <file> --dry-run

# Date range filter
python sleep_airtable_updater.py --input <file> --start-date 2026-03-01 --end-date 2026-03-22

# Start watcher manually
python sleep_watcher.py
```

## Architecture

```
sleep_watcher.py            # File system observer (watchdog)
sleep_airtable_updater.py   # Airtable sync logic
com.rick.sleep_watcher.plist  # launchd config
```

## Environment

`.env` file (copy from `.env.example`):
- `AIRTABLE_API_KEY` — Airtable Personal Access Token

Airtable target:
- Base: `appBmQA2p3z2Fdofa` (Health-Tracking)
- Table: Sleep (`tbl9nIjgF1dE079YK`)
- Dedup: by date + source

## Gotchas

- Watches iCloud path: `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/Daily-sleep/`
- iCloud can evict files — watcher has 5-second debounce
- This repo's `.venv` is shared with sync-peloton-airtable
- launchd plist is `com.rick.sleep_watcher` (inconsistent naming vs other plists)
