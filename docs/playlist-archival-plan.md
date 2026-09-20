# Playlist archival and source selection: phased plan

Scope: getting roughly 400 Spotify playlists into Music Assistant as durable
local copies, keeping those copies current, and being able to play either the
streaming or a local version of a track once local files exist.

This plan supersedes the ad hoc notes in `TODO.md` item 3a for sequencing
purposes. `TODO.md` stays the index of open questions; this file is the
execution order and the record of what was missed.

Every claim here was verified against the pinned server source (2.10.4, with
2.11.0b2 checked for comparison). Where something is unverified, it says so.

## 1. Where this actually stands

| Capability | State |
|---|---|
| Builtin playlists are local `.m3u` files | Already true, `<storage_path>/playlists/*.m3u`, so `/data/playlists/*.m3u` here |
| Bulk archive command (`playlist_bridge/archive_playlists`) | Shipped, released in v2026.09.19.9 |
| Orphaned throwaway playlist leak in `migrate_playlist` | Fixed in the same release |
| Any way to invoke the archive command | **Missing.** See 2.1 |
| Keeping archives current as Spotify changes | Not built. See phase 2 |
| Re-pointing archived entries at local files | Not built. See phase 3 |
| Playback source selection (streaming vs local) | Already shipped via `play_source_steer` plus quality ordering |
| Portable export readable by other players | Not built, still a product decision |

The single most important thing to understand about what shipped: an archive
stores *references*, not content. Each entry is a Music Assistant URI plus
`#EXTMA` metadata (name, version, ISRC, MusicBrainz id) and one `#EXTPROV`
line per provider domain. It is not a copy of the audio and not a copy of the
catalogue data needed to play anything on its own.

## 2. What we missed or got wrong

These are recorded plainly because several of them are mistakes made while
building the above, not pre-existing gaps.

### 2.1 The shipped command cannot be invoked (blocking)

`playlist_bridge/archive_playlists` has no caller anywhere.
`migrate_playlist` has a full path (`ItemContextMenu.vue` action, an eventbus
event, `MigratePlaylistDialog.vue`, `api.migratePlaylist`). The archive
command has no API method in `HA_int_MA-UI`, no menu entry and no dialog. It
was shipped, tested in CI and released without a way for a person to run it.

The cheap fix is not frontend work. A provider can render its own controls in
its settings page through `get_config_entries` returning
`ConfigEntryType.ACTION` entries, handled in `handle_config_action`. The
bundled `sonic_similarity` provider is the working precedent: it exposes
rebuild buttons that way and uses `ConfigEntryType.LABEL` entries for live
status text, with `ConfigActionResult` for feedback and `ActionUnavailable`
for a refused action. That gives a button plus status inside
`playlist_bridge`'s own settings with zero changes to the frontend repo.

### 2.2 The 2.11.0 retirement contract would now delete the archive (blocking)

`tests/test_patches.py::test_playlist_bridge_is_retired_once_the_server_pin_reaches_2_11`
instructs, on crossing the 2.11.0 pin, to "remove
`music_assistant_lm/patches/playlist_bridge.py`", and
`docs/playlist-bridge-vs-upstream.md` states the pre-decided verdict as DROP.

That contract was written when the plugin contained exactly one command that
upstream was about to ship natively. It now also contains
`archive_playlists`, which upstream does **not** provide. Following the
instruction as written would delete a feature that has no upstream
replacement.

The contract has to be split: `migrate_playlist` retires at 2.11.0, the
plugin and `archive_playlists` do not. This is rework, not a new feature, and
it should land before the pin ever moves.

### 2.3 No way to try it on a small subset first

`archive_playlists` takes only `source_provider`. There is no `limit` and no
dry run. The first ever execution against a real account is therefore all 400
playlists, roughly 15 minutes of throttled calls, with no rehearsal. Every
verification so far has been static source reading; nothing has run against a
real Spotify account.

Both parameters are small additions and should exist before the first run.

### 2.4 Nothing schedules a refresh

"Keep them current" needs something to trigger periodically. Nothing does.
`mass.tasks.register_scheduled_task` with `TaskSchedule` is available and the
builtin provider already uses it, so this is available rather than missing,
but it was never designed in.

### 2.5 Deleted-upstream detection was never considered

For a preservation use case this is arguably the most valuable signal of all,
and it is absent from every design so far. When a playlist is deleted on
Spotify, Music Assistant's library sync removes the library row. The archive
survives, which is the point. But nothing reports "this playlist no longer
exists upstream, your archive is now the only copy", which is exactly the
moment a person would want to know.

### 2.6 "Retain a copy" is weaker than it sounds

Worth stating sharply because it shapes expectations. Playlist tracks are not
synced into the library by default: `CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS`
defaults to an empty list, so playlist sync stores the playlist record only.
An archived entry therefore usually references a Spotify track that has no
library row of its own.

While Spotify is configured, entries resolve and play. If Spotify goes away,
`_resolve_playlist_item` rebuilds what it can from stored metadata and
otherwise returns an unresolved item, so the archive degrades to a visible,
ordered list of titles with ISRC and MusicBrainz ids, and no audio. That is
genuinely useful as a rebuild manifest. It is not a playable library. Phase 3
is what converts it into one.

### 2.7 Liked Songs is a special case

The Spotify Liked Songs pseudo playlist is special cased throughout the
provider (`_get_liked_songs_playlist_id`, forced global session, `me/tracks`
rather than a playlist endpoint). It can also be far larger than any single
playlist. Archival behaviour for it is untested and its size could dominate a
run.

### 2.8 Spotify playlists can contain local files

Spotify playlist items carry an `is_local` flag. Such entries have no usable
Spotify track id. Their behaviour through export and re-import is untested
and they are a plausible source of per-playlist failures.

### 2.9 Failure detail is capped

`add_task_failure` keeps at most 25 failure messages, dropping the oldest. A
run over 400 playlists with widespread failures will lose most of the detail.
The Markdown summary helps but is written only at the end, so a run that dies
mid-way leaves little behind.

### 2.10 Storage and backup growth was never sized

400 playlists at roughly 20,000 entries, each carrying `#EXTMA`, one or more
`#EXTPROV`, artist, album and image directives, lands in the low tens of
megabytes under `/data/playlists/`. That is fine on its own, but `/data` is
inside the Home Assistant app backup and nothing under `playlists/` is in
`config.yaml`'s `backup_exclude`, so every backup grows by that amount.
Whether that matters is a judgement call, but it was never measured.

## 3. Phased plan

### Phase 0: make what already shipped usable and safe

Blocking. Nothing else should happen first.

1. Split the 2.11.0 retirement contract (2.2). Narrow the tripwire to
   `migrate_playlist` and update `docs/playlist-bridge-vs-upstream.md` so the
   verdict is per command rather than per plugin.
2. Add `limit: int | None` and `dry_run: bool` to `archive_playlists`. A dry
   run should do the full eligibility pass and produce the same Markdown
   summary without exporting or importing anything.
3. Expose the command through `ConfigEntryType.ACTION` in the plugin's own
   config entries, with `ConfigEntryType.LABEL` status showing counts of
   eligible and already-archived playlists (2.1).

Exit criteria: a person can press a button, see what would happen, and run it
against a handful of playlists.

### Phase 1: first real archive run

1. Dry run. Confirm the eligibility counts match expectations, particularly
   how many playlists are owned versus followed.
2. Run with `limit` around 5. Inspect the produced `.m3u` files directly on
   disk. Confirm ISRC and MusicBrainz ids are present, order matches Spotify,
   and the entries resolve and play.
3. Check Liked Songs behaviour explicitly (2.7) and decide whether to include
   or exclude it.
4. Full run, off peak. The throttle is shared with normal playback metadata
   traffic, so this should not run while the system is in use.
5. Verify a Home Assistant backup contains `/data/playlists` and measure the
   size delta (2.10).

Exit criteria: 400 archives on disk, spot checked, and a backup that contains
them.

### Phase 2: keep the archives current

The mechanism is already available and needs no new Spotify access.

`force_refresh` sets `BYPASS_CACHE`, but `_get_data_with_caching` passes
`allow_bypass=False` and `cache.get_with_freshness` only honours the bypass
when `allow_bypass` is true. Page bodies are therefore keyed on Spotify's
ETag and stay cached across a forced refresh. Per unchanged playlist the real
cost is two requests: one `get_playlist` (which is `@use_cache()` and so is
bypassed) and one `limit=1` pagination meta call, which
`_get_playlist_pagination_meta` fetches once on page 0 and reuses for later
pages. Roughly 9 minutes for 400 playlists, with no track pages redownloaded.

1. Add a refresh mode that re-exports with force refresh, hashes the
   resulting M3U, and rewrites the archive only when the hash changed. The
   hash is our own change signal and needs no Spotify field.
2. Add a small store under `<storage_path>/playlist_bridge/` holding, per
   source playlist: archived name, last M3U hash, last checked timestamp.
   JSON is sufficient at this size and matches the convention other
   providers use for their own state. This is the extension point for
   anything tracked later.
3. Register a scheduled task to run the refresh (2.4).
4. Report playlists that were archived previously but are no longer present
   upstream (2.5), which is nearly free once the store exists.

Exit criteria: archives track upstream changes unattended, and disappearances
are surfaced rather than silent.

### Phase 3: make local files usable in the archives

This is the part with no substitute, because the server will never do it.
Verified: the 24 hourly repair pass skips any entry that already has a title,
`#EXTPROV` and `#EXTMA`, because `_stored_details_differ` compares only a
manually set name and a thumbnail and knows nothing about provider mappings,
and it unregisters itself after one clean run.
`match_imported_playlist_tracks` only considers entries whose provider is not
loaded, so while Spotify is configured it is a no operation.

1. Add a re-resolve command that walks an archived playlist, re-looks each
   entry up by ISRC or MusicBrainz id, and rewrites its `#EXTPROV` lines from
   current library state.
2. Confirm the primary URI flips to the local file. This should be automatic:
   `ProviderMapping.quality` is `audio_format.quality + priority`, which puts
   a 44.1/16 lossless file around 60 against roughly 2 for Spotify at
   320 kbps, and adds a further priority bonus for filesystem providers.
3. Confirm `play_source_steer` still forces the streaming version when asked,
   so the choice is genuinely bidirectional.

Exit criteria: an archived playlist plays from local files where they exist,
from Spotify where they do not, and can be forced either way.

### Phase 4: optional, only on demand

- Portable export that rewrites path lines to real file paths for tracks with
  a filesystem mapping, and reports what it omitted. Neither builtin `.m3u`
  nor `export_playlist` is readable by an external player today, because both
  emit Music Assistant URIs. Do this only if something outside Music
  Assistant has to read these files.
- Upgrade the phase 2 JSON store to SQLite if it outgrows a flat file.
  `aiosqlite` is already a server dependency so this adds no requirement, and
  it must be its own file rather than a table in `library.db`, which is
  removed and recreated empty on a failed migration.
- `added_at` ordering, which needs Spotify data the provider discards, and so
  is only reachable with the rejected approach in section 4.

## 4. Rejected, with reasons

### A second Spotify API client of our own

Rejected on capability, not effort. Music Assistant's provider works because
it ships an extended quota application and falls back to it: `me/playlists`
is always fetched with `use_global_session=True`, and for individual
playlists there is a per playlist fallback that marks a playlist as requiring
the global token when the user's own development mode credentials cannot read
its items. The provider says so directly: "Development Mode exposes metadata
but restricts items for non-owned playlists."

A plugin using only a personal client id would therefore be strictly less
capable than what already works. It could not enumerate playlists at all and
could not read items for anything followed rather than created. Reusing the
same client id also shares one rate budget between two throttlers that cannot
see each other, risking 429s during playback. The reimplemented auth, token
refresh and throttle stack corresponds to roughly 1300 lines upstream, and
buys about five minutes against the phase 2 approach.

Revisit only if Spotify grants extended quota to a personal application, or
if losing non-owned playlists becomes acceptable.

### A filesystem provider as the archive destination

Rejected. `add_playlist_tracks` resolves entries through
`get_track(file_path)`, so it can hold only tracks that exist as local files,
which is the opposite of this use case. Filesystem track identity is the file
path with no content hash, so reorganising a library orphans entries
silently. Both `playlist_bridge._resolve_destination` and server 2.11.0b2's
own `migrate_playlist` already refuse destinations that are neither builtin
nor a streaming provider. Full detail in `docs/decisions.md`.

## 5. Open decisions

| Decision | Needed before |
|---|---|
| Include or exclude Liked Songs from archival | Phase 1 step 3 |
| Is backup growth of tens of megabytes acceptable | Phase 1 step 5 |
| Does anything outside Music Assistant need to read these playlists | Phase 4 |
| Build phase 3 before or after acquiring local files | Phase 3 start |

On the last one, the recommendation is before. Phase 3 is inert until local
files exist, but having it ready means the first local files are picked up
immediately instead of appearing to do nothing.
