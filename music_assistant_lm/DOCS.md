# Music Assistant (Library Manager)

Same options as the official app:

| Option | Meaning |
| --- | --- |
| `log_level` | Global log level; keep `info` unless debugging. |
| `safe_mode` | Start with only the core controllers, no providers, to troubleshoot. |
| `music_drive` | A partition of the Home Assistant server that holds music. The app mounts it at `/music/<label>` for itself only. |
| `music_drive_task` | A one-off job at the next start: `backup`, `verify`, `restore` or `repair` (see below). Set it back to `none` afterwards. |

## The music drive

Pick the partition in **Music drive** and restart the app. The log shows the
mount, the label and, for exFAT, whether the volume is clean. Then add a
**Filesystem (local disk)** provider in Music Assistant and use its Browse
button to pick a folder under the mount (the fork frontend's picker; the
path can still be typed). Point the provider at a folder on the drive, not
at the mount itself, so an unmounted drive makes the provider unavailable
rather than emptying the library.

An exFAT drive that was not cleanly ejected is mounted read-only. The tasks
exist so that repairing it never risks data: run `backup` (the drive is
copied to `/share/music-drive-backup/<label>` and independently hashed),
then `repair`, then `verify` (a report of anything the repair changed) and,
if the report lists files, `restore` (copies them back from the backup).
Each task runs once at the next start and the log tells you when to set
the option back to `none`.

A successful backup contains `manifest.sha256` plus `backup-set.json`. The
completion record is published only after the source is unchanged
across the copy and every destination file matches the source snapshot. A
failed or interrupted run has no completion record, so repair and restore are
refused. Promotion keeps the previous verified set and automatically restores
it if the app lost power between directory renames. Verify, restore and repair
also validate the recovery set before using it; corruption in the backup cannot
silently become restore input.

The fork frontend's Duplicates page can move lesser copies and orphaned CUE
sheets into a trash folder on the drive (`.music-assistant-trash/` at the
root of the provider's folder, keeping each file's path). That is a rename
on the same drive: nothing is copied and nothing is deleted until you empty
the trash from that page, and every file can be restored from there until
then. It does not need the backup task.

The library manager is at `/library` in the app's own interface ("Library
manager" in the navigation). Its user guide lives in the frontend fork:
https://github.com/trooperthorn/HA_int_MA-UI/blob/main/docs/LIBRARY-MANAGER.md

## Updates

Each release of this app pins one upstream server version and one fork
frontend release; the changelog names both. Home Assistant offers the update
like any other app.
