# Coordinated Music Assistant recovery set

`scripts/recovery_set.py` packages and verifies **an already quiesced copy** of
the app's `/data` directory. Use an extracted Home Assistant backup or a copy
made while Music Assistant is stopped. It must contain `settings.json`,
`library.db`, `auth.db`, and `library_enrichment/enrichment.db`. The tool does
not stop writers, call Home Assistant backup APIs, or operate on the running
`/data` path. A file-by-file consistency check cannot make a live collection
of databases and settings atomic.

For a Home Assistant installation, stop the Music Assistant app in the Home
Assistant UI, confirm it is stopped, then create a backup containing this app.
Download the backup from the [Backups page](https://www.home-assistant.io/common-tasks/general/#backups)
(Home Assistant decrypts the download)
and extract this app's data into a private staging directory. Do not treat a
backup taken while the app was still writing as a quiesced snapshot. Record the
app, server, and frontend versions shown by the installation before stopping.

Record the versions actually installed on the source system, then run:

```powershell
python scripts/recovery_set.py create 'C:\staged-ha-backup\data' 'C:\private-recovery\ma-set' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
python scripts/recovery_set.py verify 'C:\private-recovery\ma-set' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
python scripts/recovery_set.py stage 'C:\private-recovery\ma-set' 'C:\restore-staging\data' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
```

After reviewing the staged copy on test hardware, stop the **target** app and
confirm it is stopped. The cutover command re-verifies the set and stage,
renames the existing target data directory to a new rollback path, then moves
the staged directory into place on the same filesystem:

```powershell
python scripts/recovery_set.py cutover 'C:\private-recovery\ma-set' 'C:\test-install\data' --staged-data 'C:\restore-staging\data' --rollback-data 'C:\test-install\previous-data' --confirm-stopped --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
```

`--confirm-stopped` records the operator's assertion; the script cannot check
Home Assistant's process state. It refuses corrupt staging before moving the
existing directory, restores the old directory if the swap fails, and keeps
the old directory at the rollback path after success. It never deletes that
rollback copy. Do not start the app until the cutover command succeeds.

The set contains every file from that copied data directory, exact hashes and
sizes, the three stated versions, the Library Enrichment store identity and
schema, and a completion marker bound to the manifest. Creation compares the
source before and after copying, independently checks the destination, parses
settings, and checks SQLite integrity. Verification repeats the byte and
database checks. Staging refuses an existing destination, a version mismatch,
an older or unsupported archive schema, corruption, missing files, symlinks,
and an incomplete marker. `create`, `verify`, and `stage` never replace the
target directory; only the explicitly requested `cutover` command does so.
The verifier is pinned to server 2.10.4 and library schema 58. It checks the
expected library and authentication tables and rejects a library with no schema
version, including an empty replacement database left by a failed migration.
Support for another server/schema pair requires an explicit compatibility update.

Music Assistant 2.10.4 stores its server ID and Fernet encryption key in
`settings.json`. This verifier requires both the ID and a structurally valid
key, and keeps `settings.json` and `auth.db` together with the library. Restore
those files as a unit; removing or regenerating the key can make saved provider
credentials unreadable. This check does not prove that remote service tokens
will still be accepted after a restore.

The contents may include account tokens, credentials, playback preferences,
user information, and paths. Keep the recovery set in private protected
storage. Its SHA-256 hashes detect accidental change; they are not a signature
against an attacker who can modify both the files and manifest. After a
separately reviewed cutover, check account reauthentication, local root
mappings, player groups, source preferences, and measured delay settings.

This is the offline staging, validation, and operator-controlled cutover part of
Phase 8. A live Home Assistant snapshot/cutover, live credential validation,
migration for older
schemas, and restoration on test hardware still require implementation and
validation. The separate music-drive backup does not replace this set.
