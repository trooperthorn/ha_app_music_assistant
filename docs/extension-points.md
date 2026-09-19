# Extending Music Assistant without touching server code

`music_assistant_lm/patches/*.py` has two genuinely different shapes:

- **Anchor edits** (`hass_source_select.py`, `play_source_steer.py`,
  `sendspin_opus_bitrate.py`, `browse_path.py`, `music_trash.py`): rewrite an
  exact line in an already-existing installed file. Necessary when the
  behavior lives inside a file we don't own, but fragile by construction —
  every server release that reshapes that file breaks the anchor, and the
  build (and the sync PR) fails loudly rather than shipping silently broken.
- **New providers** (`playlist_bridge.py`, see `docs/decisions.md`'s
  "Migrate Playlist ships as a new provider" entry): drop a brand-new,
  self-contained directory under the installed `music_assistant/providers/`
  package. Music Assistant discovers providers by listing directories at
  runtime (`os.listdir(PROVIDERS_PATH)` in `music_assistant/mass.py`), not
  through a central registry file, so this is purely additive: it can never
  conflict with a future upstream diff, because it never touches a line
  upstream owns.

**Default to a new provider over an anchor edit whenever the functionality
you need fits one of the three extensible provider types below.** An anchor
edit is only for changing behavior that already lives inside an existing
file we don't own (e.g. teaching an existing entity a new command). Anything
that can instead be *added* — a new source of metadata, a new way to
analyze audio, a new background capability or API command — should be a new
provider, because it survives every future server bump automatically.

This doc describes the three provider base classes that matter for that
choice, what each one can touch, real upstream examples of each, and how to
add one here the same way `playlist_bridge.py` does, all verified by
reading the pinned server's actual source
(`music_assistant/models/plugin.py`, `metadata_provider.py`,
`audio_analysis_provider.py`) and manifest schema
(`tests/test_provider_manifests.py`), not from memory or docs that may be
stale by the time this is read again.

## The common shape

Every provider type is:

1. A directory under `music_assistant/providers/<domain>/` containing at
   least `manifest.json` and `__init__.py`.
2. `manifest.json`'s required fields: `type`, `domain`, `name`,
   `description`, `codeowners` (a list — use `["@trooperthorn"]` for our own
   additions, since these are not upstream contributions). Common optional
   fields seen across real providers: `stage`, `requirements` (pip deps,
   usually `[]` for something built from what's already installed),
   `documentation`, `multi_instance`, `builtin`, `allow_disable`, `icon`.
   `type` is one of `"plugin"`, `"metadata"`, `"audio_analysis"`, `"music"`,
   `"player"` and a few others — see `script/hassfest`-adjacent validation
   in the real server's `tests/test_provider_manifests.py` for the exact
   allowed set at any given pinned version.
3. `__init__.py`: a module-level `async def setup(mass, manifest, config)
   -> ProviderInstanceType` (import `ProviderInstanceType` from
   `music_assistant.models`) that constructs and returns your provider
   instance, plus the provider class itself subclassing the matching base
   below.
4. Added to the Dockerfile's patch chain
   (`RUN ... && "$VIRTUAL_ENV/bin/python" /tmp/patches/<name>.py && ...`)
   and to the CI "Check the server patches took" step in
   `.github/workflows/test.yml`, exactly like `playlist_bridge.py`.

A patch script that adds a provider has no anchors to fail on: it can
simply (re)write the same fixed files every time, which makes it trivially
idempotent (see `playlist_bridge.py`'s `write_provider()`). Compile-check
whatever Python text you write before saving it (`compile(text, path,
"exec")`) so a typo in the patch fails the build immediately instead of
shipping a provider that can't import.

## Plugin providers (`type: "plugin"`, base class `PluginProvider`)

**What they touch**: anything that doesn't fit "provides tracks/albums" or
"analyzes audio" — background capabilities, custom API commands, exposing a
non-streaming-service audio source, or AI/TTS engines.

Concretely, a `PluginProvider` can:

- **Register arbitrary new API commands** via
  `self.mass.register_api_command(command, handler, required_scope=...)`
  in `loaded_in_mass()`. This is the mechanism `playlist_bridge.py` uses,
  and it's the most generally useful capability here: anything the frontend
  needs the server to do that isn't already a command can be added this
  way, without touching the controller that would otherwise need to own it.
- **Expose an `AudioSource`** (`get_audio_sources()`,
  `get_player_audio_sources(player_id)`, `get_stream_details(item_id,
  media_type)`), gated on `ProviderFeature.AUDIO_SOURCE`. This is how a
  plugin surfaces something playable that isn't from a music provider —
  live audio from paired hardware, a webhook-fed stream, etc. — under the
  app's normal "Live Inputs" browsing and play_media flow.
- **Expose AI or TTS engines** (`AIEngine`/`TTSEngine` in
  `music_assistant/models/plugin.py`), selectable per use the same way a
  provider config entry offers a picker.
- **React to source control events** (`on_source_control`) for
  seek/volume/shuffle/repeat/transport actions on a source it owns.

**Real upstream examples**: `party` (guest queue additions via a shareable
URL, entirely new API commands), `smart_playlist` (rule-based dynamic
playlists, new commands plus event subscriptions), `music_quiz`,
`ai_radio`, `ambient_sounds`, `sonic_similarity`, `profiler`. All in
`music_assistant/providers/<name>/` in the real server source — read
`smart_playlist/__init__.py` first, it's the clearest template for
`loaded_in_mass()` + `register_api_command` + `get_config_entries()`.

**What we've already built this way**: `playlist_bridge` (cross-provider
playlist migration, see `docs/decisions.md`).

**Where this could extend HA_int_MA-UI further, without a server patch**:
any "the frontend needs the server to do X" request that isn't a change to
existing behavior. Concretely, from the TODO history in this repo: a future
"streaming quality control exposed generically" (`TODO.md`'s item 3b) could
plausibly be a plugin command that reads/writes a per-player config value
via `get_config_entries()`/`update_config` rather than needing a new
anchor patch per player type, if the underlying player providers already
expose a way to set stream format that a plugin could call into generically
— worth checking before assuming a new anchor patch is required next time
that comes up.

## Metadata providers (`type: "metadata"`, base class `MetadataProvider`)

**What they touch**: enriching what's already in the library — artist,
album, track, and playlist metadata; similar tracks/artists; artist top
tracks/albums; recommendation rows; and resolving a provider-specific image
path/URL to actual bytes or a fetchable URL.

Every method is a no-op unless the matching `ProviderFeature` is declared
(`ARTIST_METADATA`, `ALBUM_METADATA`, `TRACK_METADATA`,
`PLAYLIST_METADATA`, `SIMILAR_TRACKS`, `SIMILAR_ARTISTS`,
`ARTIST_TOPTRACKS`, `ARTIST_TOPALBUMS`, `RECOMMENDATIONS`) — you only
implement what you declare. `priority` (lower = preferred, default 50)
controls ordering when multiple metadata providers can answer the same
request; the core metadata controller queries providers in priority order
and stops at the first usable answer, so a narrowly-scoped provider (e.g.
one that only ever has data for local/self-hosted content) should set a
low priority number to be tried first, and a broad fallback should stay at
or above the default.

**Real upstream examples**: `musicbrainz` (identity/unique-ID resolution,
`builtin: true`, cannot be disabled — everything else depends on it for
matching), `fanarttv` (artwork), `theaudiodb` (general metadata),
`lastfm_recommendations` / `lastfm_scrobble`, `genius_lyrics`. All under
`music_assistant/providers/<name>/`.

**Where this could extend HA_int_MA-UI further**: any "the library is
missing information about X" request. A metadata provider is the right
shape for pulling in data this app has that upstream metadata providers
don't reach — for example, matching against a locally-maintained
liner-notes/credits file, or a self-hosted metadata source specific to this
music drive's own tagging conventions — without needing to touch how any
existing media item is modeled or displayed; the frontend already renders
whatever `MediaItemMetadata` a provider returns.

## Audio analysis providers (`type: "audio_analysis"`, base class `AudioAnalysisProvider`)

**What they touch**: raw PCM audio as it streams, for anything computed
from the actual audio rather than its metadata — loudness normalization,
beat/tempo/key detection, crossfade timing, acoustic fingerprinting. The
same hooks run for both live playback and background library scans; a
provider does not need to know which context it's in.

The lifecycle is: `start_analysis(session_id, streamdetails, audio_format)`
(accept or reject a session; the base class already skips tracks already
analyzed at the current `analysis_version`, and skips tracks longer than
`max_analysis_duration` if you set one) → repeated
`process_pcm_chunk(session_id, pcm_chunk)` (must fully `await` its own
work, since the controller backpressures the source on it) →
`finalize(session_id)` (base-class-managed: calls your `_finalize()`,
persists the result via `set_audio_analysis`, then calls your
`post_analysis()` hook for any side effect, e.g. writing a sidecar file —
gate that on `streamdetails.path` actually being a writable filesystem
path, since this fires for background scans too). `_start_analysis` and
`_finalize` are the two abstract methods an implementation must provide.
`has_unloadable_models = True` plus `_load_models`/`_free_models` is the
pattern for a provider with a heavy ML model it wants to keep out of memory
except while actively analyzing (`ensure_models_loaded`/
`unload_idle_models` manage that automatically). All CPU-heavy work should
go through `self._run_offloaded(func, ...)`, which routes it to a thread
pool with proper concurrency/priority handling against live playback rather
than blocking the event loop directly.

**Real upstream examples**: `loudness_analysis` (EBU R128 integrated
loudness, `builtin: true`), `sonic_analysis`, `smart_fades` (crossfade
timing from the analyzed audio), `acoustid_lookup` (fingerprinting),
`_demo_audio_analysis_provider` (the intentionally minimal template — read
this one first for the smallest working example of the abstract methods).

**Where this could extend HA_int_MA-UI further**: any "compute something
from the actual audio, not its tags" request that doesn't already have a
provider — for example a provider that derives a per-track visual waveform
or spectral summary for a richer now-playing view, computed once during
background scan and cached the same way loudness already is, rather than
computed client-side on every play.

## Checklist for the next one

1. Confirm the need is additive (a new source of data/capability), not a
   change to existing behavior — if it's the latter, it's an anchor patch,
   not a new provider.
2. Pick the base class from the three above by what it fundamentally does:
   exposes a new API/audio source → `PluginProvider`; enriches existing
   library items → `MetadataProvider`; computes something from raw audio →
   `AudioAnalysisProvider`.
3. Clone the real pinned server tag and read one real example of that type
   in full before writing anything — do not write against a stale memory
   of the API shape; every field and method name in this doc was verified
   against the actual `2.10.4` source, but a later pin bump could move
   them.
4. Write the patch script (`music_assistant_lm/patches/<name>.py`) the same
   shape as `playlist_bridge.py`: a `DOMAIN` constant, string constants for
   `manifest.json` and `__init__.py`, a `locate()` that resolves the
   installed `music_assistant.providers` package via `importlib`, a
   `write_provider()` that compile-checks then writes both files, and a
   `main()` that accepts an optional path override for testing.
5. Wire it into the Dockerfile's `RUN` chain and the CI "Check the server
   patches took" step's import/assert one-liner.
6. Add tests mirroring `tests/test_patches.py`'s pattern for
   `playlist_bridge` (manifest validity, module compiles, idempotent
   writes) — this doesn't need the anchor-based idempotency/fixture-drift
   tests the anchor patches need, since there's no anchor to drift.
7. Record the design decision in `docs/decisions.md`, same shape as every
   other entry there: what triggered it, why a new provider instead of an
   anchor edit or an upstream contribution, and what it deliberately does
   not do.
