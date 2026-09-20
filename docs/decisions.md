# Decisions

## Server 2.10.4 compat: fixtures carried a latent bare-except bug (2026-09-19)

`Sync upstream pins (#45)` moved `SERVER_VERSION` from 2.10.3 to 2.10.4
without refreshing `tests/fixtures/*_2_10_3.py` or re-running
`tests/test_patches.py`, so `test_the_fixture_matches_the_pinned_server_release`
started failing (it asserts the Dockerfile pin) and the fixture stopped
matching what the app actually ships.

Refreshing the fixtures from the real `music-assistant/server` tag `2.10.4`
(fetched from `raw.githubusercontent.com`) showed only one file actually
changed: `controllers/music/controller.py` gained a `candidate_titles` CTE
and a `TRACK_RECONCILIATION_MAX_TITLE_ROWS` bound parameter in the duplicate
track query, to keep the self-join's pairing bounded per title instead of
scanning every track pair. `config/providers.py`, `player_queues/queue_loader.py`,
`streams/audio.py`, `providers/hass_players/player.py` and
`providers/sendspin/player.py` are byte-identical to 2.10.3. aiosendspin
stays pinned at 9.1.1 for 2.10.4 (the server's `requirements_all.txt`
pin did not move), so the `aiosendspin_*_9_1_1.py` fixtures needed no change.

Separately, and **not caused by the version bump**: `music_controller_2_10_3.py`,
`streams_audio_2_10_3.py` and `sendspin_player_2_10_3.py` each carried several
bare multi-exception clauses (`except A, B:` instead of `except (A, B):`),
which is not valid Python 3 syntax. Checking out the commit immediately
before the sync (`Sync upstream pins (#45)`'s parent) and running the same
suite reproduces 6 of the 7 failures already, unrelated to the pin: the
`ast.parse`/`compile` calls in `test_music_trash_*`, `test_steer_*` and
`test_opus_bitrate_*` were breaking on this pre-existing syntax before the
bump too, since PR #37 first vendored `music_controller_2_10_3.py`. None of
the three affected patches (`music_trash.py`, `play_source_steer.py`,
`sendspin_opus_bitrate.py`) touch the lines in question, so their anchors
and splices were never the problem; the vendored copies themselves were
invalid syntax. Fixed by parenthesizing the exception tuples in the
refreshed fixtures (behavior unchanged, only the grouping needed for valid
Python 3), and renaming every server-pinned fixture from `*_2_10_3.py` to
`*_2_10_4.py`. Only `test_the_fixture_matches_the_pinned_server_release`'s
failure was actually caused by the version bump; the other six were a
latent gap in test coverage that the bump happened to surface at the same
time.

**Gap in the automation.** `scripts/sync_upstream.py` resolves and writes
`SERVER_VERSION`/`SERVER_DIGEST` and the upstream app config, but it does not
run `pytest tests/test_patches.py` (or any test) against the new pin before
`sync-upstream.yml` opens its auto-merging PR, and it never touches
`tests/fixtures/`. A pin bump that breaks a patch anchor, or that ships with
a fixture that was already broken, merges to main unnoticed until someone
runs the suite by hand. This is a documentation-only observation: the sync
workflow and script are left as they are; whether to add a fixture-refresh
and `pytest tests/test_patches.py` step to `sync-upstream.yml` (failing the
auto-merge, or opening a draft PR instead) is for a human to decide.

## Build on the host, no registry image (2026-09-12)

The official app pulls `ghcr.io/music-assistant/server`; this app has no
`image:` key and lets Supervisor build the Dockerfile locally. Reason: the
image is the upstream one plus a single wheel, so publishing our own
multi-arch image would duplicate upstream's registry for no gain and add
signing and storage to maintain. Cost: a few minutes of build time on the
Home Assistant host per update.

## The image vulnerability scan reports, it does not gate (2026-09-12)

The baseline gates on high and critical findings. Here the OS layer is the
upstream Debian server image, which this repository cannot patch; a gate
would only block following the very upstream release that fixes it.
`security.yml` runs the scan with `fail-build: false`.

## The app version is CalVer, the upstream versions are arguments (2026-09-12)

Home Assistant compares app versions to decide whether an update exists.
Using the upstream server version as the app version would break when only
the frontend pin moves. CalVer via the shared release scripts keeps every
release a new version; the changelog records which server and frontend each
one carries.

## Own keys in config.yaml (2026-09-12)

`name`, `version`, `slug`, `description` and `url` are this app's; every
other key follows upstream verbatim so a change in upstream privileges or
options arrives with the next sync PR, where it is visible in the diff.

## Base images are pinned by digest, not only by tag (2026-09-13)

`FROM` and `COPY --from` carry `:<version>@sha256:...`. A tag is mutable, so
pinning only the version means an upstream re-push changes every rebuild with
no diff to review. `sync_upstream.py` resolves the multi-arch index digest for
both images on every run — not only when the version moves — so a re-publish
over the same version becomes a change in its own right and lands in a sync PR.

The tags stay alongside the digests. The daemon ignores them when a digest is
present, but they keep the file readable and the changelog meaningful.

This is also the only mitigation available for upstream's `aiolibdatachannel`
branch dependency (see security.md): the digest cannot make that pin
reproducible, but it fixes which build of it this app ships.

## backup_exclude is appended to, not owned (2026-09-13)

`webrtc_private_key.pem` has to be excluded from backups, but adding
`backup_exclude` to `OWN_KEYS` would freeze the whole list and stop upstream's
future additions arriving. Instead `EXTRA_LIST_ITEMS` appends this app's
entries to the upstream list during the merge, so upstream still owns the
contents and this app only adds. The merge is idempotent and the entry is
re-added if upstream ever drops the key.

## The server's frontend pin is recorded, not enforced (2026-09-13)

The fork wheel is installed over `music-assistant-frontend` with `--no-deps`,
so the version the server pinned for itself is gone by the time the image runs
and nothing notices the two drifting apart. `sync_upstream.py` now resolves
that pin from the server release and writes it as `SERVER_EXPECTS_FRONTEND`.

It is deliberately informational. Enforcing it is not possible from here: the
fork uses CalVer and does not record which upstream frontend release it was
built from, so there is no version to compare against. What this buys is
visibility — the pair appears in the build log, and the server's expectation
moving shows up as a line in the sync PR, which is the point at which the fork
may need a rebase. Making it enforceable means the fork publishing its upstream
base alongside the wheel; that is work for `HA_int_MA-UI`, not here.

## Server changes ship as anchored build-time edits, not a fork (2026-09-14)

The routing view needs the Home Assistant player provider to select inputs
on receiver zones, which upstream does not do. Contributing it upstream is out
(the Open Home Foundation AI policy and this repository's own rule), and
forking the server would mean owning its release train. Instead the change is
a script under `music_assistant_lm/patches/` that rewrites exact upstream
lines in the installed module during the image build. Anchoring on exact
lines is the point: when the server release changes the module, the anchor
is gone, the build fails, and the sync pull request shows it. A `patch(1)`
file would need the tool in the image and give worse errors; a sed would
apply blindly. The unit test applies the script to a vendored copy of the
upstream module pinned to the Dockerfile's server version, so the fixture
and the script move together with every server bump.

## Playback steers to the browsed source through the same mechanism (2026-09-14)

Playing from the Filesystem listing still streamed from Spotify: the server
resolves every uri to the library item and orders the stream candidates by
quality, with the playback user's provider filter as the only preference.
That filter is an account-wide access restriction, so setting it from a
listing toggle was out, and a per-play hint has no place in the API. The
`play_source_steer.py` edit reads the provider from the uri a play request
names, keeps it on the queue items as `extra_attributes.preferred_provider`
(the field the server already uses for playback speed, so it persists with
the queue), and puts it ahead of the quality order at stream time. The
frontend only has to name the source's own item in the uri it sends.

## Opus bitrate is a build-time edit of aiosendspin too (2026-09-14)

Network-aware playback (Music Assistant discussion 5264) has no upstream
code yet; the maintainers point at future Sendspin work. The fork frontend
builds its own adaptive mode on what exists: the server already changes a
player's codec in place through the preferred-format setting, the browser
player reports resyncs and sync error, and aiosendspin's transformer pool
already carries codec options. The only missing piece was the Opus
encoder ignoring those options and no setting to feed them, which is a
small anchored edit of a dependency inside the image, handled exactly like
the server edits. A fork of aiosendspin would mean owning its release
train for three lines. Fixtures are pinned to the aiosendspin version the
server release pins, so a server bump that moves aiosendspin re-checks
the anchors the same way.


## Migrate Playlist ships as a new provider, not a patch or a port (2026-09-19)

The fork frontend's "Migrate Playlist" dialog called
`music/playlists/migrate_playlist`, a command that does not exist upstream.
It traces to music-assistant/server PR #5926, authored by the project's own
lead maintainer but closed unmerged with several unresolved CRITICAL review
findings: an authorization bug (a migration task could end up reading from a
provider outside the calling user's permitted scope, because it resolved the
destination with `mass.get_provider(domain)`, which returns the first
globally loaded instance of a domain regardless of the caller's session) and
a false-success bug (the task reported a full migration even when a
destination provider silently dropped tracks). Porting that code would bring
both bugs into this fork.

Every other entry in `music_assistant_lm/patches/` edits an already-existing
installed file at an exact anchor. `playlist_bridge.py` is deliberately
different: it writes a small, self-contained plugin provider
(`music_assistant/providers/playlist_bridge/`) instead. Music Assistant
discovers providers by listing directories under its providers path at
runtime (`os.listdir(PROVIDERS_PATH)` in `mass.py`), not through a central
registry, so a new provider directory is purely additive and cannot conflict
with any future upstream diff the way an anchored edit to an existing file
can.

The plugin reuses only the parts of the upstream playlist pipeline that are
already merged and tested — `export_playlist`, `import_playlist` with
library matching (PR #3387), and the same `create_playlist`/
`add_playlist_tracks` path the frontend already uses for manual playlist
creation — and adds only the one thing genuinely missing upstream: writing
the matched tracks into the destination provider's own playlist via the
generic `MusicProvider.create_playlist`/`add_playlist_tracks` methods every
playlist-capable provider already implements. It never touches PR #5926's
matching/confidence code, so neither of its CRITICAL findings apply here:
the destination is resolved by filtering the caller's own configured
provider list (`self.mass.music.providers`) rather than a global domain
lookup, and every provider write is awaited directly in the plugin's own
background task so a real failure surfaces as this task's own failure
instead of being silently swallowed by a separately-tracked task.

`match_policy` is accepted from the existing frontend request shape but not
yet actionable: the already-merged matching pipeline this plugin calls into
does not expose a confidence knob today. Fabricating one client-side would
be dishonest about what's actually happening; this is left as a follow-up
for whenever upstream's matcher grows one.

## The PEP 758 syntax was never broken; the fixtures are restored verbatim (2026-09-19)

The "Server 2.10.4 compat" entry above concluded that the shipped server
source carried a latent bare-except bug, because `except A, B:` (without
parentheses) does not compile on the Python this repository's tooling ran
at the time. That conclusion was wrong. Music Assistant's server targets
Python 3.14 (`requires-python = ">=3.14"`, and the server's own ruff config
sets `target-version = "py314"`), and upstream deliberately adopted PEP 758
("parenthesis-free" multi-exception `except` clauses) across the codebase
in server PR #4254. `except A, B:` is valid syntax on Python 3.14 and only
invalid on 3.13 and earlier. The container this app ships runs
`python3.14`, so nothing is broken there; nothing needed fixing in the
vendored source at all.

The earlier fix parenthesized the exception tuples in
`tests/fixtures/music_controller_2_10_4.py`, `streams_audio_2_10_4.py` and
`sendspin_player_2_10_4.py`, on the false premise that upstream's own
source was invalid. Those fixtures exist to be byte-identical pinned copies
of the real upstream release, precisely so a diff against a fresh checkout
reveals genuine upstream drift; silently "fixing" them made them diverge
from upstream for no reason and would have hidden a real future change
under the same three lines. They have been restored to verbatim upstream
content from the real `music-assistant/server` tag 2.10.4.

The actual, narrower consequence for this repository is that any local
tool that parses or compiles those vendored files - this includes
`tests/test_patches.py`'s `ast.parse`/`compile` checks and the patch
scripts' own `compile()` validation - needs Python 3.14 to do it. Below
3.14, those specific tests are skipped with an explicit reason instead of
failing, so a contributor on an older local interpreter sees why, while CI
(which runs on 3.14) exercises them for real.

## `playlist_bridge` retires itself once the server pin reaches 2.11.0 (2026-09-19)

Upstream server#5989 ("cross-provider playlist migration"), authored and
merged by the project's own maintainers on 2026-09-03, registers
`music/playlists/migrate_playlist` starting in server release 2.11.0 - the
exact command name the fork frontend originally called before this
repository's `playlist_bridge` plugin took over serving it under
`playlist_bridge/migrate_playlist`. As of this writing 2.10.4 is still the
Latest stable release and 2.11.0 is nightly-only, so `playlist_bridge`
remains the only implementation available to this fork.

Because `scripts/sync_upstream.py` bumps `SERVER_VERSION` in
`music_assistant_lm/Dockerfile` automatically, nothing here would otherwise
notice the day that pin crosses into 2.11.0 and upstream's own command
becomes available. `tests/test_patches.py` carries a tripwire test next to
`test_the_fixture_matches_the_pinned_server_release` that fails as soon as
the pinned version reaches 2.11.0, with a message pointing at exactly what
to do: retire the `playlist_bridge` plugin (this includes its patch script,
Dockerfile wiring and `docs/upstream-review.md` references), and revert
`api.migratePlaylist()` in `trooperthorn/HA_int_MA-UI` back to calling
`music/playlists/migrate_playlist` directly.

The full side-by-side against upstream's merged implementation, and the
pre-decided verdict for that retirement (drop, not keep or modify), is in
[playlist-bridge-vs-upstream.md](playlist-bridge-vs-upstream.md).

## `playlist_bridge` guards ported from upstream #5989 (2026-09-19)

Upstream's merged `migrate_playlist` (server PR #5989) carries validation
this plugin initially lacked: excluding unavailable provider instances
before matching, rejecting dynamic source playlists, rejecting destinations
that are neither `builtin` nor a streaming provider, requiring
`PLAYLIST_TRACKS_EDIT` and `MediaType.TRACK` support on the destination, and
validating the destination playlist name with `is_safe_name`. All six were
verified against the real, pinned 2.10.4 server source (not just upstream's
`dev` branch) before being ported: `provider.available`,
`provider.is_streaming_provider`, `provider.supported_media_types`,
`ProviderFeature.PLAYLIST_TRACKS_EDIT`, `Playlist.is_dynamic`, and
`is_safe_name` (importable from `music_assistant.helpers.security` in
2.10.4, same as upstream's `dev`) all exist unchanged in 2.10.4. None had to
be skipped.

Upstream also validates that the *source* playlist's own provider is within
the caller's allowed, available instances, using `get_current_user()` and an
explicit `allowed_provider_instances` set built from the request's
authorization context. This plugin approximates the same intent with what
it already has: it checks whether any of the source playlist's provider
mappings are on the caller's own `self.mass.music.providers` list (already
scope-filtered for the destination check) or are `builtin`. This is not
identical to upstream's user-context-aware check and is recorded as the
plugin's weakest remaining point in
[playlist-bridge-vs-upstream.md](playlist-bridge-vs-upstream.md), rather
than presented as equivalent.

## `browse_path` moves from an anchor patch to a plugin (2026-09-19)

`browse_path.py` used to be an anchor patch: it string-replaced exact lines
in the installed `music_assistant.controllers.config.providers` module to
add a `config/providers/browse_path` command listing the folders under the
app's music roots for the fork frontend's folder picker. Reading the code
it injected made clear the anchor bought nothing: the handler had zero
`self.` references and needed nothing from that controller beyond a place
to be registered from. The anchor's only real job was delivery, and an
anchor is the fragile way to deliver something with no dependency on the
file it edits -- any upstream reshape of that module would break the build
even though the feature itself has nothing to do with the shape of that
module.

`folder_browser.py` replaces it with a plugin provider, following the same
shape `playlist_bridge.py` established: a `DOMAIN` constant, string
constants for `manifest.json`, `strings.json` and `__init__.py`, a
`locate()` that resolves the installed `music_assistant.providers` package,
a `write_provider()` that compile/json-validates before writing, and a
`main()` accepting an optional path override for tests. Music Assistant
discovers providers by listing directories under its providers path at
runtime, not through a registry, so this is purely additive and cannot
conflict with a future upstream diff the way the anchor could.

The command name (`config/providers/browse_path`) and required scope
(`Scope.CONFIG_PROVIDERS_WRITE`) carried over unchanged, so nothing
downstream had to move: `mass.register_api_command` accepts any command
string with no namespace ownership, so the fork frontend's folder picker
needed no change at all. The manifest sets `builtin: true` and
`allow_disable: false`, because this backs the Filesystem provider's
folder-picker setup flow and a user disabling it would silently break that
flow rather than fail loudly.

`register_api_command` raises `RuntimeError` if a command is already
registered, so the old anchor patch and this plugin can never both ship --
duplicate registration would fail the server at load. That same tripwire is
also what protects this plugin going forward: if a future upstream release
ever adds its own `config/providers/browse_path` command, loading this
plugin on top of it fails loudly instead of silently shadowing or
conflicting with upstream's own implementation.

This is step 1 of moving the remaining anchor-shaped-but-anchor-independent
patches to plugins; `music_trash.py`'s `library_trash` migration is next
and follows this same template.

## `music_trash` moves from an anchor patch to a plugin (2026-09-19)

`music_trash.py` was an anchor patch: it string-replaced exact lines in the
installed `music_assistant.controllers.music.controller` module to add four
`music/trash/*` commands (`move`, `list`, `restore`, `empty`) that give the
fork frontend's Duplicates page a reversible step past removing a library
row. Reading the code it injected showed the same thing `browse_path.py`
showed before it: the handlers had no real dependency on `MusicController`.
They only ever touched `self.logger` and `self.mass.get_provider`, both of
which exist on the `Provider` base class every plugin provider already
subclasses. The anchor's only job was delivery, and an anchor is the
fragile way to deliver something with no dependency on the file it edits.

`library_trash.py` replaces it with a plugin provider, following the exact
template `folder_browser.py` established (and the shape `playlist_bridge.py`
established before that): a `DOMAIN` constant, string constants for
`manifest.json`, `strings.json` and `__init__.py`, a `locate()` that
resolves the installed `music_assistant.providers` package, a
`write_provider()` that compile/json-validates before writing, and a
`main()` accepting an optional path override for tests. Music Assistant
discovers providers by listing directories under its providers path at
runtime, not through a registry, so this is purely additive and cannot
conflict with a future upstream diff the way the anchor could.

The four command names (`music/trash/move`, `music/trash/list`,
`music/trash/restore`, `music/trash/empty`) and the required scope
(`Scope.LIBRARY_MANAGE`, the same scope that already guards provider
mappings) carried over unchanged, so nothing downstream had to move:
`mass.register_api_command` accepts any command string with no namespace
ownership, so the fork frontend's Duplicates page needed no change at all.
The manifest sets `builtin: true` and `allow_disable: false`, because this
backs the Duplicates page's trash actions and a user disabling it would
silently break that flow rather than fail loudly.

`register_api_command` raises `RuntimeError` if a command is already
registered, so the old anchor patch and this plugin can never both ship;
both the anchor script and its Dockerfile `RUN` line were removed in the
same change that added the plugin.

This retires the last anchor patch on `controllers/music/controller.py`,
the single upstream file that has churned the most across server releases
so far (it is also where the retired `music_trash.py` anchor and, earlier,
several now-obsolete anchors before it, all lived). With this migration,
none of this fork's build-time edits still anchor to that file; the only
anchor patches remaining touch `hass_players/player.py`,
`controllers/player_queues/queue_loader.py` plus
`controllers/streams/audio.py`, and the `aiosendspin` package for the Opus
bitrate setting, none of which have this same anchor-with-no-real-
dependency shape, so no further plugin migrations are planned for now.

This was step 2 of 2 of moving the remaining anchor-shaped-but-anchor-
independent patches to plugins that started with `folder_browser.py`.

## Playlist import keeps the builtin destination (2026-09-19)

`TODO.md` item 3a asked whether Spotify playlist import should target a
"local playlist file on the filesystem" rather than a builtin playlist,
on the assumption that a builtin playlist is a database row. That
assumption was wrong, and the research below replaces it.

A builtin playlist already is a file on disk. `BuiltinProvider.loaded_in_mass`
sets `_playlists_dir` to `<storage_path>/playlists` and every mutation goes
through `_write_m3u_file`, which writes `<playlist_id>.m3u` there;
`get_library_playlists` enumerates the directory with `os.listdir` and treats
each `*.m3u` filename as a playlist id. This app runs the server with
`--data-dir /data`, so builtin playlists are `/data/playlists/*.m3u`, inside
the persisted volume and covered by the app backup (nothing under
`playlists/` is in `config.yaml`'s `backup_exclude`). Storing playlists as
JSON under the provider config is legacy and only read by the one-way
`_migrate_playlists` migration. Server 2.11.0b2 keeps the same layout, so
this is not a 2.10.x-only detail.

The real difference between the two destinations is what the M3U path line
holds, and what may go in it at all. Builtin writes a Music Assistant URI
(`media_item_to_playlist_item` builds it with `create_uri`) plus `#EXTMA`
carrying ISRC and MusicBrainz recording id, and `#EXTPROV` per provider
mapping. `filesystem_local.add_playlist_tracks` instead appends the provider
item id verbatim, which for that provider is a base-relative file path, with
a plain `#EXTINF` line and nothing else.

That makes the filesystem destination unusable for the case that motivated
the question. Its `add_playlist_tracks` resolves each id through
`get_track(file_path)`, so an entry can only be a track that exists as a
local file. A Spotify playlist is precisely the case where most tracks do
not, so a filesystem destination could hold only the subset already owned
locally.

The filesystem destination is also fragile in ways the builtin one is not.
A filesystem track's identity is its `relative_path` and its checksum is
only `str(int(st_mtime))`, with no content hash, so a move is seen as one
delete plus one add; `_process_deletions` then calls
`remove_item_from_library` on the old path, destroying the library row along
with its provider mappings and playlog, and cascading into album and artist
cleanup. A playlist entry that no longer resolves raises inside
`_parse_playlist_line`, is caught, logged, and skipped silently with no
placeholder. `get_playlist_tracks` caches its result for a year keyed on the
playlist file's own mtime, so moving a track does not invalidate the cached
listing. `remove_playlist_tracks` rebuilds the file from scratch as
`#EXTM3U` plus `#EXTINF` and path pairs, discarding comments, `#PLAYLIST`
and every extension tag. `create_playlist` always writes to the provider
root, and `get_playlist` forces `is_editable` false for any playlist outside
it. On the cloud and WebDAV providers `write_access` never becomes true, so
playlists there are read-only.

By contrast the builtin destination repairs itself. Matching stores ISRC and
MusicBrainz id, `_resolve_playlist_item` rebuilds from stored metadata and
falls back to a library lookup, and a scheduled pass re-enriches unresolved
or outdated entries every 24 hours. Its orphan drop is narrow: it fires only
for a path containing none of `/`, `\` or `:`, which is leftover text from a
value that once held a line break, never a track that merely failed to
match. An unmatched track therefore survives with its metadata and is
retried.

Decision: playlist import keeps the builtin destination and nothing is built
for it, because the capability the item asked for already exists. The
filesystem destination is rejected rather than deferred. Beyond the reasons
above, `playlist_bridge._resolve_destination` already refuses any
destination that is neither `builtin` nor a streaming provider, and server
2.11.0b2's own `migrate_playlist` carries the identical guard, so building a
filesystem destination would mean diverging from upstream permanently on a
decision upstream made twice.

Two genuine gaps were found while answering this, both recorded in `TODO.md`
rather than built, because each needs a product call first.

The first is portability. Both the builtin M3U files and
`music/playlists/export_playlist` emit Music Assistant URIs through the same
`media_item_to_playlist_item` and `generate_m3u` pair, so neither is readable
by an external player. That, not the destination, is what "sync to another
tool" actually needs, and it is a smaller change: rewrite path lines to real
file paths for tracks that carry a filesystem provider mapping, and report
the tracks that had to be omitted.

The second is that import matching is a fallback, not a preference.
`match_imported_playlist_tracks` only considers an entry when
`self.mass.get_provider(prov_instance)` returns nothing for the URI's own
provider, so with Spotify configured a Spotify entry is never re-resolved to
a local file. The `match_providers` argument, which this fork's frontend
already passes, filters which providers are searched once that fallback
triggers; it cannot express "prefer local even though the source provider
works". Where several providers match the same ISRC, the winner is whichever
`get_unique_providers()` yields first, which is not something this fork
controls. Closing this would suit a `playlist_bridge` command that
re-resolves a playlist after import, since that needs no server edit.

A separate SQLite database for import provenance was considered and
deferred. It is feasible and would fit the extension points: `aiosqlite` is
already a server dependency, so it would add no requirement, and
`<storage_path>/<domain>/` is the established place for provider-owned state
(the spotify, sendspin, ai_radio, smart_playlist and sonic_similarity
providers all keep files there), which also means it would land in `/data`
and be backed up. It would have to be its own file rather than a table in
`library.db`, because a failed library migration removes that database and
recreates it empty, and a user-facing action resets it too. It was deferred
because most of what it would buy already exists: unmatched entries persist
with their ISRC and MusicBrainz id and are retried every 24 hours. What it
would genuinely add is provenance, idempotent re-sync and match auditing,
which only pay for themselves if recurring incremental re-sync from a
streaming provider is wanted. Revisit it then, not before.
