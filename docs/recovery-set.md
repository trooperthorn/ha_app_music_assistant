# Coordinated Music Assistant recovery set

`scripts/recovery_set.py` packages and verifies **an already quiesced copy** of
the app's `/data` directory. Use an extracted Home Assistant backup or a copy
made while Music Assistant is stopped. It must contain `settings.json`,
`library.db`, `auth.db`, and `library_enrichment/enrichment.db`. The tool does
not stop writers, call Home Assistant backup APIs, or operate on the running
`/data` path. A file-by-file consistency check cannot make a live collection
of databases and settings atomic.

Record the versions actually installed on the source system, then run:

```powershell
python scripts/recovery_set.py create 'C:\staged-ha-backup\data' 'C:\private-recovery\ma-set' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
python scripts/recovery_set.py verify 'C:\private-recovery\ma-set' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
python scripts/recovery_set.py stage 'C:\private-recovery\ma-set' 'C:\restore-staging\data' --app-version 2026.09.22.2 --server-version 2.10.4 --frontend-version 2026.9.22.1
```

The set contains every file from that copied data directory, exact hashes and
sizes, the three stated versions, the Library Enrichment store identity and
schema, and a completion marker bound to the manifest. Creation compares the
source before and after copying, independently checks the destination, parses
settings, and checks SQLite integrity. Verification repeats the byte and
database checks. Staging refuses an existing destination, a version mismatch,
an older or unsupported archive schema, corruption, missing files, symlinks,
and an incomplete marker. The tool **never replaces the live directory**.

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

This is the offline staging and verification part of Phase 8. A coordinated
live snapshot/cutover, live credential validation, migration for older
schemas, and restoration on test hardware still require implementation and
validation. The separate music-drive backup does not replace this set.
