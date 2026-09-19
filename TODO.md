# TODO: Cross-repo UI/backend follow-ups (Music Assistant)

Tracked here because the issues below live in the bundled `music-assistant-server`
backend that this app packages, not in the frontend repo. This repo is the
right place to decide what to do about them since it owns the upstream sync
(`scripts/sync_upstream.py`, `docs/upstream-review.md`).

Source: UI workflow review requested 2026-09-19, covering
[trooperthorn/HA_int_MA-UI](https://github.com/trooperthorn/HA_int_MA-UI) and this repo.
16 workflow items were triaged; the ones below could not be fixed in either
repo as-is and need a decision or an upstream report.

## 1. Artist "All" view shows no songs for some artists (backend bug)

- **Symptom**: selecting an artist in the Library Manager UI sometimes shows
  zero tracks (e.g. "Goo Goo Dolls"), but navigating into a specific album by
  that same artist does show tracks.
- **Where it actually lives**: `HA_int_MA-UI`'s `useItemSource.ts` calls
  `api.getArtistTracks(item_id, provider)`, a single unpaginated RPC
  (`music/artists/artist_tracks`) against `music-assistant-server`. The
  album-scoped path uses a different RPC (`getAlbumTracks`) that resolves
  fine. No client-side pagination/limit is being applied in the frontend, and
  no error is being silently swallowed there.
- **Likely root cause**: a multi-provider artist identity merge gap in
  `music-assistant-server`, where the artist `item_id`/`provider` pair passed
  to `artist_tracks` doesn't match the provider instance under which that
  artist's tracks were actually indexed, while album-level lookups use the
  album's own (correct) provider linkage.
- **Decision**: do not attempt a frontend workaround (e.g. falling back to
  aggregating tracks from all albums client-side) without confirming this is
  really a server bug and not a sync/indexing gap in the specific library.
  Per this project's AI policy constraints, this needs to be reported
  upstream to `music-assistant/server` by a human (Sean), not opened
  autonomously by an agent on this project's behalf.
- **Action**: Sean to reproduce against a specific test library (confirm
  whether it's provider-specific, e.g. only affects certain streaming
  providers vs. local filesystem), then file an upstream issue against
  `music-assistant/server` with the artist/provider IDs from the API
  response, or open an issue in this repo first if it turns out to be caused
  by how `ha_app_music_assistant` configures/patches the bundled server.
- **Status**: open, unassigned.

## 2. Product decisions made instead of blocking on review (2026-09-19)

These two items were ambiguous in the original request and were resolved
with a judgment call in the `HA_int_MA-UI` PR rather than stopping to ask.
Documented here so they can be revisited if wrong.

- **"Replace Up Next" vs. "Use as Up Next" toggles**: the original request
  listed these as two separate slider controls on the bottom track table,
  but both described the same behavior (replace the Up Next queue with the
  currently shown table rows, in shown order). Merged into a single
  "Replace Up Next" control rather than shipping two controls that do the
  same thing. If a genuinely distinct second behavior was intended (e.g. one
  should preserve the currently-playing track and one should not), open a
  follow-up issue in `HA_int_MA-UI` describing the distinction.
- **"Blue bar on the left" loading expanded**: no component in `HA_int_MA-UI`
  literally matches "a blue bar on the left side" — the only persistent
  player bar is `Footer.vue`, which is full-width at the bottom of the
  screen, not left-docked. Treated the report as referring to the fullscreen
  player auto-opening on load (traced to a stale `showFullscreenPlayer` URL
  query param being honored on initial mount) and fixed that. If a different,
  actually-left-docked bar was meant, open a follow-up issue in
  `HA_int_MA-UI` with a screenshot.

## 3. Explicitly deferred (not started)

Per prior scoping conversation, these are bigger features, not workflow
fixes, and were intentionally left out of the 2026-09-19 UI fix pass:

- Import Spotify playlists into a local playlist file (MusicBrainz-assisted
  matching).
- Equalizer control (including an equalizer bar option) plus streaming
  quality/delay settings.

Both should get their own scoping pass and PR(s) in `HA_int_MA-UI` (and
possibly `ha_app_music_assistant` if server-side config is needed for the
equalizer/streaming settings) when prioritized.

## Related PRs/issues

- `HA_int_MA-UI` PR: workflow fixes batch (search debounce, title sort in
  browse scope, stale-search-on-select, scoped Up Next clear in fullscreen
  player, hover tooltips, double-click-plays-first-song, Replace Up Next
  toggle, fullscreen player load-minimized fix) — see that repo's PR list
  for the branch `ui-workflow-fixes`.
