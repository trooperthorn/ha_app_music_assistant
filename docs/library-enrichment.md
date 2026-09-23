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
- An administrator with library-write permission can preview and explicitly apply
  one committed version as a visible builtin playlist. The projection preserves
  ordered duplicate Spotify track references, reports every omitted occurrence,
  verifies the stored M3U order, and records its applied checkpoint separately.
- Apply intent is durable and idempotent. Once playlist creation starts, an
  unverified failure becomes `uncertain` and is never retried automatically.
- Each subscription can remain manual or use an explicit 1-hour-to-7-day
  snapshot check interval. Scheduled checks reuse the initiating administrator's
  current permissions and provider scope, skip item retrieval when the source
  snapshot is unchanged, and commit a new immutable version when it changes.
- Synchronization jobs and access failures are durable. Authentication or access
  loss pauses future checks until an administrator saves the schedule again;
  pausing to manual remains available while the source provider is offline.
- Each subscription has an explicit revisioned playback policy: `prefer_local`,
  `local_only`, or `prefer_spotify`. Preview uses only explicitly approved v4
  local candidates, reports missing, ambiguous, rejected, and unsupported gaps,
  and preserves occurrence order and duplicates without rewriting the archive.
- Playback projection is a separate, explicit write with digest and policy-revision
  preconditions. Its durable checkpoint is independent of archive-copy apply.
  Synchronization never creates or updates a playback playlist.
- A maintained mirror is separately opt-in per source subscription. It writes a
  builtin playlist of ordered Spotify track references after a complete committed
  source capture, and skips a destination write when projected content is unchanged.
  Its own checkpoint records the last applied version and the exact destination
  fingerprint. Archive refresh still succeeds when a mirror write fails. External
  playlist edits stop mirror updates; explicit detach preserves that playlist and
  permits a later new destination. Uncertain writes are never retried automatically.
  On restart, a pending external write is marked uncertain. Reconciliation reads
  the exact builtin playlist under its lock and verifies either the target or
  previous content before changing the checkpoint. An explicit abandon records
  that an orphaned playlist may remain; it never deletes the playlist.
- Read-only provenance extracts a bounded, typed Spotify field set from the
  committed immutable payload without refreshing Spotify or Music Assistant.
- That provenance read can append a version-bound snapshot of MusicBrainz
  recording, release-track, release, release-group and ordered artist-credit
  identities already present in the Music Assistant library. It uses only the
  existing Spotify provider mapping and never searches or refreshes a provider.
- Provider administrators can correct an observed provenance field with an
  expected revision, or clear the correction. The original observation remains
  intact and later source reads do not remove the correction.
- Playlist item provenance links known builtin archive, playback and mirror destinations
  to their subscription, snapshots, and durable check state.
- A staged legacy iTunes XML export can be inspected with bounded parsing and
  explicit path mappings. The source digest, selection and preview are durable;
  this slice performs no Music Assistant library write.
- A staged ZIP can transport that XML compactly. The authenticated small-upload
  route accepts XML-only ZIPs; inspection maps their old paths against existing
  Music Assistant media without extracting or duplicating audio.

These are metadata/reference archives, not downloaded audio. A visible applied
playlist contains Spotify references and does not establish local playback. This
slice does not automatically update an archive-copy playlist or playback
projection, support Liked Songs, automatically approve local
matches, or replace coordinated
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
| `library_enrichment/provenance` | `version_id`, optional `limit` (1..200, default 100), `offset` | occurrence-aligned typed provenance and effective values; no inline raw payload |
| `library_enrichment/set_provenance_override` | `version_id`, `source_item_id`, `field_name`, JSON `value`, `expected_revision` | revision-checked administrator correction for an observed field in that version |
| `library_enrichment/clear_provenance_override` | `version_id`, `source_item_id`, `field_name`, `expected_revision` | revision-checked clear, restoring the observed effective value |
| `library_enrichment/item_provenance` | `media_type` (`playlist`), `library_item_id` | builtin destination linkage and current, source/capture, mirror conflict/uncertain/detached, or unknown state |
| `library_enrichment/itunes_inspect` | `library_path` under the advertised staging directory | bounded XML inventory, stable identities, playlist classifications and old path roots; no MA library write |
| `library_enrichment/itunes_preview` | `inspection_id`, `source_digest`, explicit provider-bound `path_mappings`, selected `playlist_ids` | durable digest-bound actual MA library resolution and ordered preview; no write |
| `library_enrichment/itunes_apply` | `inspection_id`, `revision`, `source_digest`, `preview_digest`, one `playlist_id`, optional `allow_partial` | explicitly create and verify one ordered builtin playlist; also requires `library.write` |
| `library_enrichment/itunes_apply_status` | `inspection_id` | durable one-playlist apply state and destination without retrying mutation |
| `library_enrichment/cancel` | `job_id` | final durable state; an already executing atomic commit can win the cancellation race |
| `library_enrichment/apply_preview` | `version_id` | exact projection digest, source/projected counts, omissions, partial-consent requirement and existing destination |
| `library_enrichment/apply` | `version_id`, `expected_digest`, optional `allow_partial` | create and verify one visible builtin playlist; also requires `library.write` |
| `library_enrichment/apply_status` | `version_id` | durable apply state and destination without starting or retrying a write |
| `library_enrichment/sync_policy` | `subscription_id` | current revisioned manual/scheduled policy and durable sync state |
| `library_enrichment/set_sync_policy` | `subscription_id`, `expected_revision`, `mode`, optional `interval_seconds` | atomically change or pause the schedule |
| `library_enrichment/sync_now` | `subscription_id` | queue one explicit snapshot-aware check and return its job/task IDs |
| `library_enrichment/sync_status` | `subscription_id` | policy, state, recent jobs and latest job |
| `library_enrichment/match_review` | `version_id`, optional `limit` (1..200, default 100), `offset` | occurrence-aligned local candidates from existing merged mappings, including ambiguity and freshness |
| `library_enrichment/set_match_decision` | `version_id`, `source_item_id`, `expected_revision`, `action` (`approve`, `reject` or `clear`), `asset_id` for approval or rejection | revisioned decision overlay; also requires `library.write` |
| `library_enrichment/relocate_match_asset` | `version_id`, `source_item_id`, approved `asset_id`, `provider_instance_id`, `old_item_id`, `new_item_id`, `expected_revision`, optional `provisional_asset_id` | verifies the current MA local mapping, then carries an approval to a reviewed file move without changing MA mappings; also requires `library.write` |
| `library_enrichment/playback_policy` | `subscription_id` | current mode, revision, actor and update time |
| `library_enrichment/set_playback_policy` | `subscription_id`, `mode`, `expected_revision` | CAS update only; never writes a playlist |
| `library_enrichment/playback_preview` | `version_id` | ordered rows, explicit fallbacks, visible gaps, counts, policy revision and digest |
| `library_enrichment/playback_apply` | `version_id`, `expected_digest`, `expected_policy_revision`, optional `allow_partial` | explicitly write and verify the builtin playback projection; also requires `library.write` |
| `library_enrichment/playback_status` | `subscription_id` | policy plus independent durable projection state and destination |
| `library_enrichment/playback_detach` | `subscription_id`, `expected_destination_item_id`, `expected_content_digest` | release ownership of an edited playback destination without deleting its playlist; also requires `library.write` |
| `library_enrichment/mirror_status` | `subscription_id` | separate revisioned mirror state, last applied version, destination and failure |
| `library_enrichment/mirror_preview` | `subscription_id` | current committed source projection, order, omissions and digest; no write |
| `library_enrichment/mirror_configure` | `subscription_id`, `enabled`, `allow_partial`, `expected_revision` | opt in or pause with compare-and-swap; also requires `library.write` |
| `library_enrichment/mirror_apply` | `subscription_id`, `expected_version_id`, `expected_digest` | explicitly create or update the verified builtin mirror; also requires `library.write` |
| `library_enrichment/mirror_detach` | `subscription_id`, `expected_destination_item_id`, `expected_content_digest` | preserve the destination playlist and release it from automatic updates; also requires `library.write` |
| `library_enrichment/mirror_reconcile_preview` | `subscription_id`, optional `candidate_item_id` | read a candidate builtin playlist and classify its content against the uncertain target and previous verified destination |
| `library_enrichment/mirror_reconcile` | `subscription_id`, `candidate_item_id`, `expected_revision`, `expected_target_digest`, `expected_observed_content_digest` | re-read under the builtin playlist lock; commit a verified target or mark unchanged previous content retryable; also requires `library.write` |
| `library_enrichment/mirror_abandon_uncertain` | `subscription_id`, `expected_revision`, `expected_target_digest` | explicitly stop an uncertain mirror without deleting a possible orphan playlist; also requires `library.write` |

Local match review is an overlay on the immutable Spotify occurrence archive. It
uses direct MA library lookups and only considers mappings owned by an available,
non-streaming provider. It does not search providers, refresh metadata, add or
remove MA mappings, or alter playback selection. Repeated source occurrences stay
separate in the response while sharing one account-scoped source decision. A
failed library read returns the last stored candidates as stale when available;
it is not retried automatically. See `local-match-review.md` for the exact
contract and current boundary.

Capabilities advertise `provenance_read`, `provenance_api_version: 1`,
`provenance_override_api_version: 1`,
`max_provenance_page: 200`, `raw_payload_inline: false`, `item_provenance`, and
`item_provenance_api_version: 1`. `musicbrainz_identity_api_version: 1` advertises
the additive local-library identity snapshot; `local_catalog_api_version: 1`
advertises safe label, barcode, artwork source, and audio-format snapshots.
See `provenance.md` for field and
state details.

Capabilities advertise `itunes_import`, `itunes_import_api_version: 1`,
`itunes_apply`, `itunes_apply_api_version: 1`, the 10,000-occurrence apply limit,
and the server-side staging directory. Only XML files inside
that directory are readable. The parser rejects DTD/entity declarations, binary
plist payloads, duplicate dictionary keys and over-limit inputs. `.itl` and
`.itdb` databases are not accepted. A preview requires an unchanged SHA-256,
explicit longest-prefix path mappings and at least one importable playlist. See
`itunes-xml-import.md` for the current boundary.

`prefer_local` selects an approved local URI and otherwise records an explicit
Spotify fallback. `prefer_spotify` selects Spotify and falls back to an approved
local URI only when the occurrence has no usable Spotify identity. `local_only`
omits unresolved occurrences, requires explicit partial consent when omissions
exist, and writes `#EXTPROV:local_only||<provider-instance>` before each local
entry. The server playback hook converts that sentinel into a strict provider
constraint; it must never retry another provider.

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
`ArchiveStore.verify_backup(path)` checks the published manifest, byte hash,
SQLite integrity, schema, identity and every immutable version without opening
the live store. `ArchiveStore.stage_restore(source, new_path)` copies a verified
backup into a new staging path and repeats those checks on the copied bytes;
it refuses an existing path and never replaces the live database. A 10,000-item
archive exercises this path in tests. Coordinated Music Assistant data/config
restore still requires a separate cutover procedure and a live recovery test.

## Validation and promotion

Fixtures cover source account collisions and rename, unknown schema rejection,
duplicate/null occurrence restoration, failed B capture retaining committed A,
source changes and pagination gaps, account/session changes, cancellation,
authorization including queued/revoked users, and absence of inspection refresh.
App CI imports the installed plugin in the pinned server image. Before enabling on
a real installation, complete image CI and a selected-source test plus independent
backup/restore proof. Keep historical versions and archive data during upgrades or
provider removal; do not substitute a new empty database after a migration error.

Next work: uncertain-apply reconciliation, broader read-only inspection UI,
source retention controls and a coordinated recovery set. Match review now records
evidence and explicit decisions; playback-policy enforcement still requires a
server stream-resolution integration.
