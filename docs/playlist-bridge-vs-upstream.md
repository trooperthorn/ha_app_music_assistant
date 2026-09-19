# playlist_bridge vs upstream #5989

The verdict, stated up front: once the pinned server reaches 2.11.0, drop
`playlist_bridge`. Do not keep it and do not modify it into a permanent
fixture. Upstream's own `migrate_playlist` (server PR #5989, merged
2026-09-03, shipping in server 2.11.0) is a strict superset of what this
plugin does and is more thoroughly hardened. There is no capability this
plugin has that #5989 lacks. This document exists so that decision is a
two-minute read at retirement time, not a re-investigation.

## Why the bridge exists, and what it covers

The fork frontend calls `music/playlists/migrate_playlist`. That command
does not exist in server 2.10.x. It traces to an earlier, closed PR (#5926)
with unresolved CRITICAL findings (a provider-scope-escape bug and a
false-success bug); porting that code would have imported both bugs into
this fork. `playlist_bridge` is a small, self-contained plugin provider that
serves the same command name by orchestrating only already-merged, already
tested upstream building blocks (`export_playlist`, `import_playlist` with
library matching, and the generic `create_playlist`/`add_playlist_tracks`
path).

This plugin covers the server 2.10.x line only. Upstream lands its own,
real implementation of the same command in server 2.11.0, which as of
2026-09-19 is nightly-only (2.10.4 is Latest stable; a `backport/2.10.5`
branch exists but does not carry the migration). `playlist_bridge` therefore
has to run in production for however long 2.10.x remains the pin, which is
why it was hardened with the guards below rather than left as a throwaway
shim.

## Behaviour, side by side

**Destination resolution and scope checks.** Both resolve the destination
by filtering the caller's own configured provider list
(`self.mass.music.providers`), matching on instance id first and then
domain, rather than a global `get_provider(domain)` lookup. This is the fix
for the scope-escape bug on the closed #5926. Both now exclude unavailable
provider instances before matching. Where they differ is the *source*
side: upstream additionally resolves the source playlist's own provider
through `get_current_user()` and an explicit `allowed_provider_instances`
set built from the request's authorization context, so a source outside the
caller's session is rejected with the same precision as the destination
check. This plugin approximates that by checking whether any of the source
playlist's provider mappings are on `self.mass.music.providers` or are
`builtin`, using the same primitives the destination check already relies
on. That is a reasonable approximation given what the plugin has access to,
but it is not identical to upstream's user-context-aware check, and it has
not been proven equivalent under every provider topology (for example, a
provider instance shared across multiple users) the way upstream's has been
covered by its own test suite. Treat this as the plugin's weakest point.

**Guards.** Both enforce, in the same order of intent: reject dynamic
source playlists, reject destinations that are not `builtin` and not a
streaming provider, require `PLAYLIST_CREATE` or `PLAYLIST_CREATE_TRACKS`,
require `PLAYLIST_TRACKS_EDIT`, require `MediaType.TRACK` in
`supported_media_types`, and validate the destination name with
`is_safe_name`. This plugin ported all of these from upstream's merged
implementation and verified each symbol exists in the pinned 2.10.4 source
before using it (see the commit that added them). None were skipped.

**`match_policy` handling.** Upstream implements this for real: a typed
`PlaylistMatchPolicy` enum defaulting to `SAME_RECORDING`, which actually
constrains which candidate matches the matching pipeline accepts. This
plugin's `match_policy` parameter is accepted only so the existing frontend
request shape keeps working; it has no effect. The already-merged matching
pipeline this plugin calls into (`import_playlist` /
`match_imported_playlist_tracks`) exposes no confidence knob, so every
value produces the same best-effort metadata match. This is stated plainly
in the plugin's own docstring rather than faked. A separate, later change
to the frontend disables the match policy selector in the UI; that change
lives in a different repository and is not part of this plugin.

**Duplicate handling.** Neither implementation de-duplicates tracks already
present in the destination playlist before appending; both simply append
whatever the matching pass produced. This is parity, not a gap either way,
but it is worth knowing about if a future report of duplicate tracks after
migration comes in against either implementation.

**Migration reporting and progress.** Upstream runs the whole migration as
one managed background task with its own progress and outcome reporting
integrated into the task controller's standard machinery, including
distinguishing a partial migration (some tracks matched, others did not)
from a total failure. This plugin also runs as a background task
(`run_background_task`), but its outcome is coarser: it reports overall
success, or raises `MusicAssistantError` if zero tracks matched or the
destination rejected the write, without a distinct "partial" outcome
surfaced to the caller. A playlist that migrates 8 of 10 tracks looks like
a plain success here, where upstream's richer reporting could in principle
surface the count. This is a real gap relative to upstream, not a
stylistic difference.

**Failure semantics.** Both await the destination provider's write
directly inside the task handler rather than delegating to a separately
queued, separately tracked background task, which is what closes #5926's
false-success bug: a real per-item failure here surfaces as this task's own
failure. This is parity.

## The verdict, and why

Upstream #5989 is a strict superset: it has every guard this plugin has,
plus a real, user-context-aware source-provider check this plugin only
approximates, plus a real `match_policy`, plus richer progress and partial
outcome reporting, all covered by upstream's own test suite
(`tests/controllers/music/test_playlist_migration.py`). Keeping
`playlist_bridge` around after 2.11.0, or trying to "modify" it into a
long-term parallel implementation, would mean maintaining a strictly worse
version of a capability upstream already maintains, for no benefit. Retire
it. The retirement steps are already tracked in
`tests/test_patches.py`'s `test_playlist_bridge_is_retired_once_the_server_pin_reaches_2_11`
and in `docs/decisions.md`:

1. Remove `music_assistant_lm/patches/playlist_bridge.py`, its
   `docs/upstream-review.md` references, and its `COPY`/`RUN` lines in
   `music_assistant_lm/Dockerfile`.
2. In `trooperthorn/HA_int_MA-UI`, revert `api.migratePlaylist()` to call
   `music/playlists/migrate_playlist` directly instead of
   `playlist_bridge/migrate_playlist`.
3. Remove the plugin's registered API command
   (`playlist_bridge/migrate_playlist`) references from any remaining
   frontend or documentation text.

## What would change this verdict

The verdict is pre-decided but not unconditional. Re-check it, rather than
following it blindly, if any of the following turns out to be true when
2.11.0 actually becomes the pin:

- Upstream's 2.11.0 `migrate_playlist` regressed one of the guards listed
  above (check `tests/controllers/music/test_playlist_migration.py` in the
  server release actually being pinned, not just the `dev` branch this
  document was written against).
- Upstream's implementation dropped or weakened the scope-escape fix (the
  destination and source provider resolution described above), reopening
  the original #5926 authorization bug in a new form.
- This fork depends on a capability `playlist_bridge` has that upstream's
  version does not carry (none is known as of this writing; the tables
  above list every point of comparison found).

If none of these hold, retire the plugin on schedule.
