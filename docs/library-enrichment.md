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
- Imported Spotify playlist listing uses library summaries only. It neither
  contacts Spotify nor triggers media refresh. Live offset pages are candidates,
  not a complete account inventory or a stable snapshot of concurrent library edits.
- Optional capture preconditions bind a request to the account and snapshot shown
  in the user's preview. A mismatch fails before writing archive state.
- Bounded historical version listing returns metadata without loading occurrences.

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
| `library_enrichment/sources` | `provider_instance_id`, optional `limit` (1..200, default 100), `offset` | imported library candidates, excluded reasons, page boundaries and `has_more` |
| `library_enrichment/capture` | same as preview; optional `expected_account_id`, `expected_snapshot_id` | durable `job_id`, `subscription_id`, MA `task_id`; stale preview rejected |
| `library_enrichment/status` | none | persisted subscriptions, checkpoints, jobs, counts and sanitized failures |
| `library_enrichment/version` | `version_id` | immutable ordered occurrence capture, verified against content digest |
| `library_enrichment/versions` | `subscription_id`, optional `limit` (1..200, default 50), `offset` | newest-first version metadata; stable timestamp/ID ordering |
| `library_enrichment/cancel` | `job_id` | final durable state; an already executing atomic commit can win the cancellation race |

The playlist ID is the 22-character source ID, not a URL, title or MA integer ID.
The provider instance ID must identify a currently available Spotify instance in
the requesting user's provider scope. A preview writes no archive state, hydrates
no MA media and schedules no enrichment. Normal provider authentication can still
refresh and persist credentials; it is not a claim of zero filesystem writes.
Captures reuse existing request throttling/session selection. A token expiring
inside a request retry may fail safely; rerun capture after resolving authentication.
Preview establishes metadata/size eligibility only; `items_access_verified` remains
false until capture actually reads the pages. Invalid raw entries are retained with
an explicit invalid state rather than silently represented as null source entries.

The companion frontend adds selected-capture controls to Library Enrichment's
provider settings. It checks source-listing and preview-precondition capabilities,
requires a fresh preview when selection or limit changes, and sends the reviewed
account/snapshot back with capture. Older backends must not silently fall back to
the legacy bulk bridge. Status includes persisted progress after navigation/restart.
Cancellation can return `state: stopping, cancelled: false`; keep checking status
instead of reporting success prematurely. A cancelled capture request during SQLite
preparation waits for the thread and fails its durable job before releasing the
lifecycle lock, so it cannot leave an orphan pending job that blocks the next attempt.

The backup helper `ArchiveStore.backup(new_path)` is currently an internal tested
contract, not a web command accepting arbitrary filesystem paths. It refuses an
existing destination, verifies destination integrity and version digests, and
writes `<new_path>.manifest.json` only after independently reopening the destination
and verifying its schema/identity/content. The complete manifest is fsynced and
published atomically without overwriting an existing file; the current helper needs
hard-link support on the backup filesystem and fails safely when unavailable.
Tests reopen this copy and recover repeated/null
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

Next work: builtin mirror apply and reconciliation, broader read-only inspection
UI, source retention controls and a
coordinated recovery set. Matching and enrichment follow those preservation gates.
