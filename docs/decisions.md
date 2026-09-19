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

