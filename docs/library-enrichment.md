# Library Enrichment implementation slice

This experimental, opt-in plugin starts the phased preservation roadmap. It is
compatible only with Music Assistant 2.10.4 and its Spotify provider private hooks.
The image build installs it; an administrator must add Library Enrichment as a
provider before its commands are registered. It never auto-starts a capture.

## Implemented boundary

- One explicitly selected Spotify playlist per request, maximum 10,000 occurrences.
- Stable `(provider domain, authenticated source account, playlist ID)` identity.
  Playlist names and MA IDs do not determine archive identity. Instance context is
  retained, and an instance reinstall can deliberately rebind the same source.
- Independent SQLite at `<MA data directory>/library_enrichment/enrichment.db`.
  Foreign keys, bounded busy timeout, serialized writes, initial schema fingerprint,
  store UUID and fail-closed unsupported-schema handling. No writes to MA tables.
- Every zero-based source occurrence survives, including repeats, nulls, local
  tracks, episodes, unavailable entries and unknown shapes, with explicit states.
- Fresh pre/post snapshots, strict pagination totals/offsets, account checks and
  atomic commits. Observed, attempted and committed checkpoints are separate.
- Durable job progress and failures. Restart marks pending work interrupted; an
  explicit new capture retries from a fresh source observation. No automatic retry.
- Immutable versions with content digests, and an extension-only SQLite backup
  helper that verifies the copied database and writes a digest manifest.
- Admin-only commands, including reads. Shared Spotify cards do not grant archive
  access. Background workers refresh the initiating user's permissions and bind
  that user explicitly because MA's task queue does not restore request context.
- Direct library inspection avoids the normal refresh-on-access `get()` method.

These are metadata/reference archives, not downloaded audio. This slice does not
apply MA builtin mirrors, schedule sync, expose a bulk UI, support Liked Songs,
perform local matching, enforce playback source policies, or replace coordinated
MA backup/restore. It does not change ordinary detail-page behavior. The existing
bridge bulk copy remains a separate name-based export and is not a durable archive.
Production retention/source-policy controls, storage verification and live recovery
remain release gates. Do not treat the roadmap's Phase 0–2 gates as complete.

## Command contract (API version 1)

All commands require `config.providers.write` plus an authenticated user. Call
through the authenticated MA API; do not pass Spotify credentials. Frontends must
check `library_enrichment/capabilities` and hide dependent controls when unavailable.

| Command | Arguments | Result |
| --- | --- | --- |
| `library_enrichment/capabilities` | none | compatibility, frontend/server versions, limits and supported features |
| `library_enrichment/inspect` | `media_type`, `library_item_id` | full existing library item, `external_ids_loaded: true`; no provider fallback |
| `library_enrichment/preview` | `provider_instance_id`, `source_playlist_id`, optional `max_items` | source name, snapshot, count, authenticated account and instance identity |
| `library_enrichment/capture` | same as preview | durable `job_id`, `subscription_id`, MA `task_id` |
| `library_enrichment/status` | none | persisted subscriptions, checkpoints, jobs, counts and sanitized failures |
| `library_enrichment/version` | `version_id` | immutable ordered occurrence capture, verified against content digest |
| `library_enrichment/cancel` | `job_id` | final durable state; an already executing atomic commit can win the cancellation race |

The playlist ID is the 22-character source ID, not a URL, title or MA integer ID.
The provider instance ID must identify a currently available Spotify instance in
the requesting user's provider scope. A preview writes no archive state, hydrates
no MA media and schedules no enrichment. Normal provider authentication can still
refresh and persist credentials; it is not a claim of zero filesystem writes.
Captures reuse existing request throttling/session selection. A token expiring
inside a request retry may fail safely; rerun capture after resolving authentication.

The backup helper `ArchiveStore.backup(new_path)` is currently an internal tested
contract, not a web command accepting arbitrary filesystem paths. It refuses an
existing destination, verifies destination integrity and version digests, and
writes `<new_path>.manifest.json`. Tests reopen this copy and recover repeated/null
occurrences. No live library restore or coordinated MA recovery has been attempted.

## Validation and promotion

Fixtures cover source account collisions and rename, unknown schema rejection,
duplicate/null occurrence restoration, failed B capture retaining committed A,
source changes and pagination gaps, account/session changes, cancellation,
authorization including queued/revoked users, and absence of inspection refresh.
App CI imports the installed plugin in the pinned server image. Before enabling on
a real installation, complete image CI and a selected-source test plus independent
backup/restore proof. Keep historical versions and archive data during upgrades or
provider removal; do not substitute a new empty database after a migration error.

Next work: capability-aware selection/status controls, builtin mirror apply and
reconciliation, broader read-only inspection UI, source retention controls and a
coordinated recovery set. Matching and enrichment follow those preservation gates.
