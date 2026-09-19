"""
Add a plugin that migrates a library playlist's tracks to another provider.

The fork frontend ships a "Migrate Playlist" dialog that calls
``music/playlists/migrate_playlist``. That command does not exist upstream:
it traces to music-assistant/server PR #5926 ("Migrate playlists between
providers"), authored by the project's own lead maintainer but closed
unmerged with several unresolved CRITICAL review findings from an automated
reviewer, including a real authorization bug (a migration task could end up
reading from a provider outside the calling user's permitted scope) and a
false-success bug (claims a full migration when a destination provider
silently dropped tracks). Porting that code as-is would import those bugs
into this fork.

Every other patch in this directory edits an already-existing installed
file at an exact anchor. This one is different on purpose: instead of
patching around the closed PR's bugs, it adds a small, self-contained
**plugin provider** that reuses only the parts of the upstream playlist
pipeline that are already merged and tested (``export_playlist``,
``import_playlist`` with library matching, and the generic
``MusicProvider.create_playlist``/``add_playlist_tracks`` used by the
existing, safe ``music/playlists/create_playlist`` and
``add_playlist_tracks`` commands), plus the one thing genuinely missing
upstream: pushing the matched tracks into the destination provider's own
playlist. See ``playlist_bridge/__init__.py`` for exactly what it does and
does not reuse from the closed PR.

Music Assistant discovers providers by listing directories under its
providers path at runtime (``os.listdir(PROVIDERS_PATH)`` in
``music_assistant/mass.py``), not through a central registry file. Adding a
new provider directory is therefore purely additive: it cannot conflict
with any future upstream diff to an existing file, unlike an anchor-based
edit. This script has no anchors to fail on; it always (re)writes the same
two files.

Usage: ``python playlist_bridge.py [path/to/music_assistant/providers]``.
Without a path the installed providers package is located through
``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

DOMAIN = "playlist_bridge"

MANIFEST_JSON = """\
{
  "type": "plugin",
  "stage": "stable",
  "domain": "playlist_bridge",
  "name": "Playlist Bridge",
  "description": "Migrate a library playlist's tracks into another provider's own playlist.",
  "codeowners": ["@trooperthorn"],
  "requirements": [],
  "documentation": "https://github.com/trooperthorn/ha_app_music_assistant/blob/main/docs/upstream-review.md",
  "multi_instance": false,
  "builtin": true,
  "icon": "swap-horizontal"
}
"""

STRINGS_JSON = """\
{
  "manifest": {
    "description": "Migrate a library playlist's tracks into another provider's own playlist."
  },
  "background_task": {
    "playlist_bridge_migrate": "Migrate playlist {0} to {1}"
  }
}
"""

INIT_PY = '''\
"""
Migrate a library playlist's tracks into a destination provider's own playlist.

Added by trooperthorn/ha_app_music_assistant (see
music_assistant_lm/patches/playlist_bridge.py for why this is a plugin
rather than a patch, and docs/upstream-review.md for the security
comparison against the closed upstream PR this replaces).

This deliberately does NOT port music-assistant/server PR #5926's matching
engine: that PR's confidence-scoring code (in compare.py) had unresolved
CRITICAL review findings including a provider-scope-escape bug and a
false-success bug, and porting it would bring those bugs into this fork.
Instead this plugin orchestrates only already-merged, already-tested
upstream building blocks:

- ``export_playlist`` / ``import_playlist(library_matching=True)`` (server
  PR #3387) to get a library-scoped, matched copy of the source playlist.
- The generic ``MusicProvider.create_playlist`` / ``add_playlist_tracks``
  methods every playlist-capable provider already implements, via the
  same safe controller commands the frontend already uses for manual
  playlist creation (``music/playlists/create_playlist`` and
  ``add_playlist_tracks``).

The destination provider is resolved by filtering the CALLING USER's own
configured provider list (``self.mass.music.providers``) by instance id
then by domain, rather than calling ``self.mass.get_provider(domain)``
directly. The latter returns the first globally loaded instance of a
domain regardless of whether the caller's session is actually scoped to
it, which is exactly the authorization bug flagged on the closed PR.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Callable
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.background_task import BackgroundTask
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Playlist
    from music_assistant_models.provider import ProviderManifest

# playlist creation needs at least one of these; PLAYLIST_CREATE is deprecated
# in favour of PLAYLIST_CREATE_TRACKS but some providers still only report it
PLAYLIST_CREATE_FEATURES = (ProviderFeature.PLAYLIST_CREATE, ProviderFeature.PLAYLIST_CREATE_TRACKS)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PlaylistBridgeProvider(mass, manifest, config)


class PlaylistBridgeProvider(PluginProvider):
    """Bridges a library playlist's matched tracks into another provider's playlist."""

    _unregister_handles: list[Callable[[], None]]

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_handles = [
            self.mass.register_api_command(
                "playlist_bridge/migrate_playlist",
                self.migrate_playlist,
                required_scope=Scope.LIBRARY_WRITE,
            )
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands when the provider is unloaded."""
        for unregister in getattr(self, "_unregister_handles", []):
            unregister()

    def _resolve_destination(self, destination_provider: str) -> MusicProvider:
        """
        Resolve a caller-supplied destination to a provider the caller may use.

        Filters the caller's own configured provider instances rather than
        looking the domain up globally, so a request cannot target a
        provider instance outside the caller's own configuration. This is
        the fix for the scope-escape bug flagged on the closed upstream PR.
        """
        for provider in self.mass.music.providers:
            if provider.instance_id != destination_provider and provider.domain != destination_provider:
                continue
            if not isinstance(provider, MusicProvider):
                msg = f"{destination_provider} is not a music provider"
                raise InvalidDataError(msg)
            # mirror the core create_playlist check exactly: PLAYLIST_CREATE is
            # deprecated in favour of PLAYLIST_CREATE_TRACKS but some providers
            # (filesystem_local among them) still declare only the old one, and
            # the core controller accepts either for a track playlist. Requiring
            # the new flag on its own would reject destinations core allows.
            if not any(feature in provider.supported_features for feature in PLAYLIST_CREATE_FEATURES):
                msg = f"{provider.name} does not support creating playlists"
                raise InvalidDataError(msg)
            return provider
        msg = f"{destination_provider} is not one of your configured providers"
        raise ProviderUnavailableError(msg)

    async def migrate_playlist(
        self,
        db_playlist_id: str,
        destination_provider: str,
        match_policy: str = "best_effort",
        name: str | None = None,
    ) -> BackgroundTask:
        """
        Queue migrating a library playlist's tracks to a destination provider's own playlist.

        :param db_playlist_id: Library playlist id to migrate.
        :param destination_provider: Instance id or domain of a provider the
            caller is configured to use.
        :param match_policy: Accepted for frontend compatibility. The
            already-merged upstream matching pipeline this plugin calls into
            does not yet expose a confidence knob, so every policy value
            currently gets the same best-effort metadata match; a real
            policy distinction is a follow-up once upstream's matcher
            exposes one, not something this plugin should fabricate.
        :param name: Optional name for the new playlist; defaults to the
            source playlist's name.
        """
        # resolve and validate before scheduling any work
        destination = self._resolve_destination(destination_provider)
        playlist = await self.mass.music.playlists.get_library_item(int(db_playlist_id))
        del match_policy  # accepted, not yet actionable; see docstring
        return self.mass.tasks.run_background_task(
            name=f"Migrate playlist {playlist.name} to {destination.name}",
            handler=lambda: self._migrate_playlist(playlist, destination, name),
            translation_key="playlist_bridge_migrate",
            translation_owner=self.translation_owner,
            translation_args=[playlist.name, destination.name],
            metadata={
                "task_domain": "playlist_bridge_migrate",
                "playlist_id": str(playlist.item_id),
                "playlist_name": playlist.name,
                "destination_provider": destination.instance_id,
            },
            allow_retry=True,
            priority=True,
        )

    async def _migrate_playlist(
        self, playlist: Playlist, destination: MusicProvider, name: str | None
    ) -> None:
        """Do the actual export, match and cross-provider write."""
        builtin = self.mass.get_provider("builtin")
        if builtin is None or not isinstance(builtin, MusicProvider):
            msg = "builtin provider is not available"
            raise ProviderUnavailableError(msg)

        m3u_data = await self.mass.music.playlists.export_playlist(playlist.item_id)
        # a throwaway builtin copy, matched against the destination provider only;
        # awaited directly rather than through import_playlist's background-task
        # wrapper so this handler controls sequencing without polling the tasks
        # controller
        matched_playlist = await builtin.import_playlist(m3u_data)
        await builtin.match_imported_playlist_tracks(
            matched_playlist.item_id, [destination.instance_id]
        )

        # the destination provider's own track ids, kept as-is: a provider item_id
        # may itself contain slashes (a filesystem provider's are relative paths),
        # so they must never be round-tripped through a uri and split back out
        matched_item_ids: list[str] = []
        async for item in self.mass.music.playlists.tracks(matched_playlist.item_id, "builtin"):
            for mapping in item.provider_mappings:
                if mapping.provider_instance == destination.instance_id:
                    matched_item_ids.append(mapping.item_id)
                    break

        if not matched_item_ids:
            msg = (
                f"No tracks in '{playlist.name}' could be matched against "
                f"{destination.name}; nothing was migrated"
            )
            raise MusicAssistantError(msg)

        # create_playlist is the same safe, tested path the frontend already
        # uses for manual playlist creation (music/playlists/create_playlist)
        new_playlist = await self.mass.music.playlists.create_playlist(
            name or playlist.name, [MediaType.TRACK], destination.instance_id
        )
        new_prov_mapping = next(
            (m for m in new_playlist.provider_mappings if m.provider_instance == destination.instance_id),
            None,
        )
        if new_prov_mapping is None:
            msg = f"{destination.name} did not report a playlist id for the new playlist"
            raise MusicAssistantError(msg)

        # call the provider directly and await it, rather than the queued
        # add_playlist_tracks background task, so a real per-item failure here
        # surfaces to this task's own outcome instead of being silently
        # swallowed by a separately-tracked task - the false-success bug this
        # plugin exists to avoid.
        try:
            await destination.add_playlist_tracks(new_prov_mapping.item_id, matched_item_ids)
        except MusicAssistantError as err:
            msg = f"{destination.name} rejected the migrated tracks: {err}"
            raise MusicAssistantError(msg) from err
'''


def locate() -> Path:
    """The installed providers package directory."""
    import importlib.util

    spec = importlib.util.find_spec("music_assistant.providers")
    if spec is None or spec.origin is None:
        raise SystemExit("music_assistant.providers is not installed")
    return Path(spec.origin).parent


def write_provider(providers_dir: Path) -> None:
    """Write the plugin's manifest, strings and module, creating its directory if needed."""
    import json

    provider_dir = providers_dir / DOMAIN
    provider_dir.mkdir(exist_ok=True)
    manifest_path = provider_dir / "manifest.json"
    strings_path = provider_dir / "strings.json"
    init_path = provider_dir / "__init__.py"
    compile(INIT_PY, str(init_path), "exec")
    json.loads(STRINGS_JSON)
    manifest_path.write_text(MANIFEST_JSON, encoding="utf-8", newline="\n")
    strings_path.write_text(STRINGS_JSON, encoding="utf-8", newline="\n")
    init_path.write_text(INIT_PY, encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    providers_dir = Path(argv[1]) if len(argv) > 1 else locate()
    write_provider(providers_dir)
    print(f"playlist_bridge: installed to {providers_dir / DOMAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
