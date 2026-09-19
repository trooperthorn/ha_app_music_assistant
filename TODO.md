# TODO: Cross-repo UI/backend follow-ups (Music Assistant)

Tracked here because the issues below touch the bundled `music-assistant-server`
backend that this app packages, or need a decision that spans both this repo
and `HA_int_MA-UI`. This repo owns the upstream sync
(`scripts/sync_upstream.py`, `docs/upstream-review.md`).

History:
- 2026-09-19: 16 UI workflow items triaged from a review of `HA_int_MA-UI`
  and this repo; most were fixed directly, the rest recorded below.
- 2026-09-19 (later same day): deep-dive research against
  `music-assistant/server` (all issues/PRs) and `music-assistant/support`,
  and against the actual pinned server source, resolved most of what was
  still open below. See per-item notes for what was verified and how.

## 1. Artist "All" view shows no songs for some artists — FIXED

Was: "backend bug, needs upstream report." **Confirmed not a server bug** by
reading `music_assistant/controllers/music/media/artists.py` in the actual
pinned 2.10.4 server source: `music/artists/artist_tracks` is intentionally
library-only for a library-scoped artist and does not fall back to the
provider catalog (matches upstream
[server#4039](https://github.com/music-assistant/server/pull/4039), merged
2026-06-08, which deliberately separated library-artist views from
per-provider listings). An artist whose albums are library items but whose
individual tracks aren't (e.g. "Goo Goo Dolls") shows zero tracks by design,
not by bug.

Fixed in `HA_int_MA-UI` — see
[PR #56](https://github.com/trooperthorn/HA_int_MA-UI/pull/56): browse-scope
artist-tracks fetch now falls back to the cross-provider top-tracks listing
(`getArtistTopTracks`) when the library-scoped result is empty, matching the
pattern already used on the artist detail page
(`src/components/artist/artistData.ts`).

No upstream issue needed; the original "Sean to file an upstream issue"
action is dropped.

**Related upstream context, not blockers, kept for reference:**
- [server#6324](https://github.com/music-assistant/server/pull/6324) (open,
  draft) is a real but separate multi-provider identity-merge problem
  (same-provider artists with different IDs, e.g. Tidal "loud" vs "LOUD").
- [server#6327](https://github.com/music-assistant/server/pull/6327) (closed,
  unmerged) — an automated repair pass for that was rejected as unsafe
  because some providers legitimately list two distinct artists under
  near-identical names.
- [server#6362](https://github.com/music-assistant/server/pull/6362) (merged
  2026-09-15) fixed library items picking up a bogus `"None"` provider link;
  already included in our pinned 2.10.4, no action needed.
- [server#6279](https://github.com/music-assistant/server/pull/6279) (closed,
  unmerged) — do not build against a `music/artists/discography` command,
  it does not exist upstream; a maintainer rejected merging provider
  catalogs for this on data-quality grounds.

## 2. Product decisions made instead of blocking on review (2026-09-19)

- **"Replace Up Next" vs. "Use as Up Next" toggles**: merged into a single
  "Replace Up Next" control (`HA_int_MA-UI`,
  `src/library-manager/LibraryManagerView.vue`), since both described the
  same behavior. Documented in code at the call site. No action unless a
  genuinely distinct second behavior is wanted later.
- **"Blue bar on the left" loading expanded**: treated as the fullscreen
  player auto-opening from a stale `showFullscreenPlayer` URL query param,
  and fixed (`src/layouts/default/Default.vue`). **Still open**: no
  left-docked persistent bar exists anywhere in `HA_int_MA-UI` today (the
  only persistent player bar is full-width at the bottom). If a different,
  actually-left-docked bar was meant, this needs a screenshot from Sean
  before anything else can be done here.

## 3. Deferred features — rescoped 2026-09-19

### 3a. Import Spotify playlists — smaller than originally scoped

The matching engine this needs largely already exists upstream:
[server#3387](https://github.com/music-assistant/server/pull/3387) (merged
2026-03-30) added `music/playlists/export_playlist` (to M3U8) and
`music/playlists/import_playlist` with tiered library matching (exact
ISRC/MusicBrainz match first, fuzzy fallback, optional provider
restriction), plus
[server#5986](https://github.com/music-assistant/server/pull/5986) (merged)
which re-matches tracks if the original import source later disappears.
`HA_int_MA-UI` already exposes all of this end to end
(`ImportPlaylistDialog.vue`, `ItemContextMenu.vue`'s M3U8 export,
`api.importPlaylist`/`api.exportPlaylist`).

**What's actually still missing**: importing into a **local playlist file on
the filesystem** specifically, as opposed to a builtin/library playlist.
Upstream's import destination is always a builtin playlist, not an arbitrary
`.m3u` on disk.

**Decision needed (Sean)**: is a builtin-playlist destination good enough,
or does the filesystem-file destination specifically matter (e.g. for
syncing to another tool)? If the latter, scope a small filesystem-write step
on top of the existing import pipeline — much smaller than the originally
assumed "build Spotify OAuth + MusicBrainz matching from scratch," since
matching is already solved.

**Status**: waiting on Sean's decision above; not blocked on any missing
server capability.

### 3b. Equalizer — DONE, was already fully shipped before this was written

Fully shipped both upstream (DSP/parametric EQ landed
[server#1795](https://github.com/music-assistant/server/pull/1795) in
2024-12, multichannel PEQ, gain/balance, high/low-pass, convolution, stereo
width, crossfeed, limiter, compressor, transpose all merged by 2026-07/08)
and in `HA_int_MA-UI` (`src/components/dsp/*`, `EditPlayerDsp.vue`). The only
real gap — no quick-access entry point outside Settings — was closed in
[PR #55](https://github.com/trooperthorn/HA_int_MA-UI/pull/55) (equalizer
button + menu entry in the fullscreen player, opens the existing DSP
editor). No further action.

### 3b'. Streaming quality / delay — partially shipped, rest is optional

- Playback delay already exists as a per-player setting in `HA_int_MA-UI`
  (`audio_delay` in `src/helpers/player_menu_items.ts` and
  `player_menu_preferences.ts`) — the original "no delay capability exists"
  assumption was wrong, this was already there.
- General streaming-quality override does **not** exist upstream beyond one
  narrow case:
  [server#5882](https://github.com/music-assistant/server/pull/5882)
  (merged) added a quality setting, but it's Spotify Connect-specific only.
  This repo's own `music_assistant_lm/patches/sendspin_opus_bitrate.py`
  patch is the only other precedent, and it's Sendspin/Opus-specific too.
- **Status**: optional, no upstream API to build a general version against
  today. Scope separately only if a specific player type's quality control
  is actually wanted; there's no one-size-fits-all RPC to wrap.

## 4. `MigratePlaylistDialog` called a server command that did not exist — FIXED

Found 2026-09-19 (not from the original workflow review). `HA_int_MA-UI`
shipped a full "Migrate Playlist" UI
(`src/layouts/default/MigratePlaylistDialog.vue`, reachable from
`ItemContextMenu.vue`'s `migrate_playlist.action`) calling
`api.migratePlaylist()` →  `music/playlists/migrate_playlist`, a command that
does not exist in the pinned 2.10.4 server. Anyone who opened that dialog and
submitted it got a runtime failure. The feature traced to upstream
[server#5926](https://github.com/music-assistant/server/pull/5926), which was
closed unmerged with unresolved authorization and false-success findings, so
porting it as-is was rejected.

**Fixed** by shipping the `playlist_bridge` plugin provider
(`music_assistant_lm/patches/playlist_bridge.py`), which registers
`playlist_bridge/migrate_playlist` and orchestrates only the already-merged,
already-tested parts of the upstream playlist pipeline, and by repointing
`api.migratePlaylist()` at that command in `HA_int_MA-UI`. Note this
supersedes the earlier statement here that no patch in this repo adds the
command: one now does.

**This is a bridge for the 2.10.x line only.** Upstream landed its own
reworked version in
[server#5989](https://github.com/music-assistant/server/pull/5989) (merged
2026-09-03) under the original `music/playlists/migrate_playlist` name,
shipping in server 2.11.0. Upstream's implementation is a strict superset and
more thoroughly hardened, so the decision when the pin crosses 2.11.0 is to
drop ours rather than keep or modify it. The full side-by-side comparison,
the retirement steps, and what would change that verdict are in
`docs/playlist-bridge-vs-upstream.md`; `tests/test_patches.py` carries the
tripwire that fails the build at that pin bump.

## 5. `gh pr merge --auto` races the checks it is supposed to wait on — FIXED

Both `.github/workflows/sync-upstream.yml` (around line 135) and
`.github/workflows/prepare-release.yml` (around line 163) create their
automation PR and immediately call
`gh pr merge "$PR_NUMBER" --auto --squash --delete-branch`. If the PR's own
checks (this repo's `Test` workflow) have not registered against the PR yet
at that moment, the call fails with
`GraphQL: Pull request is in unstable status (enablePullRequestAutoMerge)`.

**Symptom**: a green automation PR is created but the auto-merge enable
call fails silently in the workflow logs, so nothing ever merges it and it
sits open. This happened for real on 2026-09-19 to a sync-upstream PR and
had to be merged by hand.

**Fix (2026-09-19)**: both workflows now retry the `gh pr merge --auto`
call, up to 6 attempts with a 10s sleep between them, instead of failing on
the first "unstable status" response. Every attempt failing still fails the
step loudly (with the manual `gh pr merge` command to run) so a genuine
problem is never silently swallowed.

## Notes for anyone contributing upstream later

- Per standing project policy, no PRs or issues get opened on non-`trooperthorn`
  repos autonomously; upstream gaps get documented here as follow-ups, and
  Sean files anything that needs to go to `music-assistant/server` himself.
- `music-assistant/server` has almost no user-filed issues; user reports go
  to `music-assistant/support` — search both when checking a symptom.
- No `wontfix`/`not planned` labels are in use upstream; a rejected idea
  shows up as a closed-unmerged PR with a maintainer comment instead.
- Upstream has an automated critical-issue gate that holds PRs in draft
  until flagged threads are resolved; maintainers bypass it with an
  `override-critical` label. Worth knowing when judging whether an open
  upstream PR is actually close to merging.

## Related PRs

- `HA_int_MA-UI` #54: original workflow fixes batch (search debounce, title
  sort in browse scope, stale-search-on-select, scoped Up Next clear in
  fullscreen player, hover tooltips, double-click-plays-first-song, Replace
  Up Next toggle, fullscreen player load-minimized fix).
- `HA_int_MA-UI` #55: equalizer quick-access button.
- `HA_int_MA-UI` #56: artist-tracks fallback fix (item 1 above).
- `ha_app_music_assistant` #48: fixed a patch-compatibility break against
  server 2.10.4 unrelated to the above (pre-existing invalid syntax in
  pinned test fixtures, exposed by an automated version-pin bump that
  didn't run the patch test suite before merging — see `docs/decisions.md`).
