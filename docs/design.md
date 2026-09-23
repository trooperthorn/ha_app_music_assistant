# Design

## The problem

The Music Assistant server installs its frontend as the PyPI package
`music-assistant-frontend`, pinned to an exact version in the server's
`pyproject.toml`, and serves it from that package's install directory
(`music_assistant_frontend.where()`). A frontend fork therefore cannot reach
a running Music Assistant by any route except replacing that package inside
the server's environment. The official app pulls a prebuilt registry image,
so there is no hook to do that either.

## The shape

One app, `music_assistant_lm`, whose Dockerfile is:

1. `FROM ghcr.io/music-assistant/server:<SERVER_VERSION>`, the exact image the
   official app runs.
2. `uv` copied in (the final server image ships a venv without pip).
3. The fork's wheel downloaded from a GitHub release of
   `trooperthorn/HA_int_MA-UI`, checked against a pinned SHA-256, and
   installed with `--no-deps --force-reinstall` over the upstream package.
4. The server's own entrypoint, unchanged.

The build also records `SERVER_EXPECTS_FRONTEND`, the frontend version the
server release pins for itself, and prints it beside the installed fork
version. Because the install uses `--no-deps`, that expectation is otherwise
invisible once the image is built; see decisions.md for why it is recorded
rather than enforced.

Supervisor builds this image on the Home Assistant host when the app is
installed or updated (there is no `image:` key), so no registry, image
signing or multi-arch publishing is needed on this side. The cost is a few
minutes of build time on the host per update and the pull of the upstream
image.

Everything else in `music_assistant_lm/` is the upstream app definition:
`config.yaml` (with this app's own `name`, `slug`, `description`, `url` and
`version`), `apparmor.txt` and `translations/en.yaml`.

## Build-time edits of the server

`music_assistant_lm/patches/` holds small Python scripts that edit the
installed server in place while the image builds, one script per change,
each documenting what it changes and why. The Dockerfile runs every script
after the frontend wheel is installed. A script is a list of exact upstream
lines and their replacements; it refuses to run when an anchor is missing or
ambiguous, so a server release that reshapes the edited module fails the
image build and the sync pull request instead of shipping without the change.
Each script is a no-op on a module it already edited.

Today there are fourteen: ten anchor patches that rewrite exact lines in an
already-installed file, and four plugin providers that add a self-contained
new provider directory instead (see `docs/extension-points.md` for the
distinction).

Anchor patches:

- `hass_source_select.py`: the Home Assistant player provider mirrors the
  wrapped entity's `source_list` as selectable player sources (which gives
  the player `select_source` on the server side), mirrors its `source` as
  `extra_attributes.hass_source`, and implements `select_source` as
  `media_player.select_source`. The fork frontend's routing view (`/flow`)
  switches receiver and amplifier zones to the Chromecast feed input with it.
- `play_source_steer.py`: a play request whose uri names a provider instance
  (`filesystem_local--xyz://track/...`) marks the queue items it produces with
  `extra_attributes.preferred_provider`, and stream resolution tries that
  provider before the quality order (the same way the playback user's
  provider filter already is), falling back to the others when it cannot
  serve the item. Without it every uri resolves to the library item and the
  stream comes from the best-quality provider, so a track that is on Spotify
  and on disk streams from Spotify even from the Filesystem listing. The
  fork frontend's library manager sends such uris for a listing narrowed to
  one source.
  A caller that requires local playback prefixes a direct play uri with
  `local-only:`. A builtin M3U entry uses
  `#EXTPROV:local_only||<provider-instance>` because server 2.10.4 parses the
  raw path but reconstructs playlist tracks from their provider mappings,
  so a path prefix alone is not preserved into queue loading. Both forms set
  `extra_attributes.strict_provider` on the resulting queue item. The named
  provider must be loaded, available, and non-streaming; initial selection,
  cached details, and capacity retries stay inside that instance/domain, and
  provider-match discovery is disabled. Invalid or unserviceable strict
  requests fail instead of widening to a streaming provider.
- `sendspin_opus_bitrate.py`: a per-player `sendspin_opus_bitrate` setting
  (bits per second, 0 for the encoder default) for Sendspin players. It
  edits aiosendspin as well as the server: the Opus encoder applies the
  `bit_rate` codec option it already accepts, the player role passes the
  option to the transformer pool and the stream requirements and
  re-announces the stream when it changes, and the provider exposes and
  applies the setting next to the preferred format. The fork frontend's
  web player uses it as the lower rungs of its adaptive mode and from the
  phone layout's menu. Its fixtures are pinned to the aiosendspin release
  the server pins (9.1.1 for 2.10.3).
- `sendspin_cast_delay.py`: exposes the Cast receiver's 0–5000 ms
  `sendspin_static_delay` setting on its derived Sendspin player before the
  receiver connects. The pinned Cast bridge already sends the saved value as
  `syncDelay` at launch and after configuration changes, but its provisional
  role does not advertise the generic delay command, hiding the setting while
  idle. Other bridges retain capability-gated delay controls.
- `sendspin_cast_status.py`: publishes bounded Cast receiver state transitions
  (`connecting`, `connected`, `playing`, `stopped`, `error`, `disconnected`) on
  its derived Sendspin player's `extra_attributes.sendspin_cast_state` for
  HTTP/ingress clients. On failure it adds a bounded
  `sendspin_cast_failure` code (`device_unavailable`, `launch_timeout`,
  `launch_failed`, `receiver_error`, or `audio_unsupported`) and resolves the
  pending play request promptly. It suppresses duplicate events and does not
  expose receiver log text.
- `sendspin_timing_status.py`: copies only client-reported output delay,
  startup lead, and minimum buffer values to the Sendspin player's event state.
  Missing reports remain unknown, and a replacement connection clears old
  reports rather than treating a saved configuration value as an acknowledgement.
- `sendspin_controller_switch.py`: advertises the controller `switch` command
  already handled by the pinned aiosendspin server. It cycles playing groups
  and prioritizes rejoining the client's previous group after a leave; the
  browser SDK still sends it only when advertised by the server.
- `sendspin_discovery_status.py`: adds an admin-scoped, read-only
  `sendspin/discovery_status` command. It reports listener, advertising,
  client-discovery, manual-address validity, and connected-client status
  without returning pairing credentials or altering connections. Its
  `api_version: 1` advertises the response shape to newer frontend wheels.
  The same patch provides `sendspin/display_capabilities` at player-control
  scope; the frontend checks its `browser_display_pairing` flag before opening
  a display connection during a frontend-first release.
- `sendspin_non_audio_clients.py`: classifies artwork, color and controller-only
  clients as non-audio devices, so they cannot be selected as speakers.
  A browser `Music Assistant Display` client is account-paired through the
  existing web pairing command and kept private to its authenticated session.
- `sendspin_source_status.py`: adds an admin-scoped, read-only status command
  for connected source clients, reported signal, selected destination, PCM
  activity, and the configured latency target. Its `api_version: 1` gates the
  frontend source controls during frontend-first releases. It does not claim an acoustic
  end-to-end latency measurement.

Plugin providers:

- `playlist_bridge.py`: a plugin provider registering a
  `playlist_bridge/migrate_playlist` command that migrates a library
  playlist's tracks into another provider's own playlist, reusing only the
  already-merged, safe parts of the upstream playlist pipeline
  (`export_playlist`, `import_playlist` with library matching, and the
  generic `create_playlist`/`add_playlist_tracks`). It exists because the
  fork frontend's "Migrate Playlist" dialog calls a command upstream never
  shipped in a mergeable form (server PR #5926, closed unmerged with
  unresolved authorization and false-success bugs); a reworked version
  merged upstream as PR #5989 for server 2.11.0, so this plugin is a bridge
  for the 2.10.x line only and retires itself once the pin crosses 2.11.0
  (`tests/test_patches.py` carries the tripwire; see `docs/decisions.md`).
- `folder_browser.py`: a plugin provider registering a
  `config/providers/browse_path` command that lists the folders under the
  app's music roots (`/music`, the drive this app mounts; `/media`;
  `/share`), each path checked with the server's own `is_safe_path` against
  those roots so nothing else in the container can be listed, under the
  same scope that saves a provider config. The fork frontend's folder
  picker calls it from the Filesystem provider's setup and reconfigure
  flows, so the path is picked rather than typed. The server has no
  directory listing of its own outside a configured provider. Originally an
  anchor patch on the providers config controller; moved to a plugin
  because the handler had no dependency on that controller (see
  `docs/decisions.md`).
- `library_trash.py`: a plugin provider registering four `music/trash/*`
  commands (`move`, `list`, `restore`, `empty`) that give the fork
  frontend's Duplicates page one reversible step past removing a library
  row. `move` renames a file of a Filesystem provider into
  `.music-assistant-trash/` at the root of that provider's folder, keeping
  its relative path; same filesystem, so no copy is made and the sync skips
  the dot folder. `restore` renames it back and refuses when the original
  path is taken again. `empty` is the only command that deletes anything.
  Every path is checked with the server's own `is_safe_path` against the
  provider's folder, the provider must have a `base_path` (the Filesystem
  family), and the commands carry the scope that already guards provider
  mappings (`LIBRARY_MANAGE`). The server itself never touches files on
  disk. Originally an anchor patch on the music controller; moved to a
  plugin because the handlers had no dependency on that controller beyond
  `self.logger` and `self.mass.get_provider`, both available on the base
  `Provider` class (see `docs/decisions.md`). This retires the last anchor
  patch on `controllers/music/controller.py`.
- `library_enrichment.py`: installs the archive and enrichment provider with
  versioned captures, iTunes import, match review, playback policy, and
  recovery commands. Its data store and operations remain separate from the
  bulk playlist migration/export plugin.

`tests/test_patches.py` applies each script to a copy of the upstream modules
kept under `tests/fixtures/` and pins the copies to the server version in the
Dockerfile, so a server bump is the moment the fixtures and the anchors are
re-checked.

## The music drive

`music_drive` (an app option of schema type `device(subsystem=block)`, a
drop-down of the host's partitions) names a drive that holds music.
Supervisor binds the host's `/dev` into every app and grants access to the
chosen partition through a cgroup rule, and the app's AppArmor profile
(upstream's, needed for the SMB provider's own in-container mounts)
already permits `mount` and `umount` with `SYS_ADMIN`. So the drive is
mounted inside this container only, natively through the host kernel's
driver (HAOS ships exFAT, NTFS, FAT, ext4, btrfs and xfs as modules, loaded
by the kernel on the first mount), with no re-export and no other app
seeing it.

`rootfs/usr/local/bin/mass_with_drive.py` is the image's entrypoint. With
no drive configured it hands over to the server image's own entrypoint
unchanged. With one it waits for the node, reads the filesystem type and
label from the superblock, mounts the partition at `/music/<label>`
(`nosuid,nodev,noexec,relatime`, plus `umask=000,iocharset=utf8` for
filesystems without ownership), runs the server entrypoint as a child,
forwards the stop signal, and unmounts after the server exits. A mount
failure or a missing drive is logged and the server starts without it. A
Filesystem provider is meant to point at a folder under the mount, so an
unmounted drive makes the provider unavailable instead of presenting an
empty library that would mark every track missing.

Nothing writes to the drive on its own. An exFAT volume gets a read-only
check at every start; one that was not cleanly ejected is mounted read-only
and the log says so. Writes happen only through `music_drive_task`, one
task per start, each reporting under `/share/music-drive-backup/<label>`:
`backup` (rsync copy plus independent source/destination SHA-256 snapshots,
published only when the source stayed stable and the copy matches, with the prior
set retained for crash recovery during promotion), `verify` (the drive
against the manifest, a report of missing, changed and new files),
`restore` (the reported files copied back from the backup) and `repair`
(the exFAT check with repair, run before the mount and refused without a
valid completed recovery set, so a verified backup always precedes it). The wrapper is tested in
`tests/test_drive_wrapper.py` with the host commands stubbed.

## Two upstreams and one fork

`scripts/sync_upstream.py` reads three things and writes the pins:

- `music-assistant/server`: latest non-prerelease `X.Y.Z` release tag.
- `trooperthorn/HA_int_MA-UI`: latest `vYYYY.MM.DD.N` release, its wheel and
  its `SHA256SUMS` line (the digest is computed from the wheel if the sums
  file is absent).
- `music-assistant/home-assistant-addon` `music_assistant/`: config,
  AppArmor and translations, merged so this app's own keys win.

The fork publishes that wheel with its `publish-fork.yml` workflow on every
merge to its `main`.

## Versioning

The app version is CalVer `YYYY.MM.DD.N` in `config.yaml`, written only by
`scripts/set_version.py` and validated by `build_release_artifacts.py`
(the shared release scripts). The upstream server version and the frontend
release are Dockerfile arguments and appear in the app changelog, not in the
app version.
