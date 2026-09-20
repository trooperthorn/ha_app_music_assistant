"""
Add a plugin that migrates a library playlist's tracks to another provider.

The fork frontend ships a "Migrate Playlist" dialog that calls
``music/playlists/migrate_playlist``. That command does not exist in the
server release this app pins: it first traced to music-assistant/server PR
#5926 ("Migrate playlists between providers"), authored by the project's own
lead maintainer but closed unmerged with several unresolved CRITICAL review
findings from an automated reviewer, including a real authorization bug (a
migration task could end up reading from a provider outside the calling
user's permitted scope) and a false-success bug (claims a full migration when
a destination provider silently dropped tracks). Porting that code as-is
would import those bugs into this fork.

Upstream later landed a reworked version in server PR #5989 (merged
2026-09-03) under that same command name, shipping in server 2.11.0. This
plugin is therefore a bridge for the 2.10.x line only, and is retired when
the pin crosses 2.11.0. ``tests/test_patches.py`` carries the tripwire that
fails the build at that point; see docs/decisions.md.

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
three files (``manifest.json``, ``strings.json`` and ``__init__.py``).

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
    "playlist_bridge_migrate": "Migrate playlist {0} to {1}",
    "playlist_bridge_archive": "Archive playlists ({0})"
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

from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    set_current_task_report,
    update_current_task_progress_from_index,
)
from music_assistant.helpers.security import is_safe_name
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
            ),
            self.mass.register_api_command(
                "playlist_bridge/archive_playlists",
                self.archive_playlists,
                required_scope=Scope.LIBRARY_WRITE,
            ),
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
        # mirrors upstream #5989: self.mass.music.providers only applies user
        # scope filtering, so an unavailable instance would otherwise be
        # selected here and only fail once the background task actually
        # runs. Exclude unavailable instances before matching, preserving
        # instance_id-then-domain precedence.
        available_providers = [item for item in self.mass.music.providers if item.available]
        provider = next(
            (item for item in available_providers if item.instance_id == destination_provider),
            None,
        ) or next(
            (item for item in available_providers if item.domain == destination_provider),
            None,
        )
        if provider is None:
            msg = f"{destination_provider} is not one of your configured providers"
            raise ProviderUnavailableError(msg)
        if not isinstance(provider, MusicProvider):
            msg = f"{destination_provider} is not a music provider"
            raise InvalidDataError(msg)
        # mirrors upstream #5989: migration destinations are limited to Music
        # Assistant itself or an actual streaming provider, so a migration
        # cannot silently duplicate into a fixed local catalog it doesn't own.
        if provider.domain != "builtin" and not provider.is_streaming_provider:
            msg = "Playlists can only be migrated to Music Assistant or a streaming provider"
            raise InvalidDataError(msg)
        # mirror the core create_playlist check exactly: PLAYLIST_CREATE is
        # deprecated in favour of PLAYLIST_CREATE_TRACKS but some providers
        # (filesystem_local among them) still declare only the old one, and
        # the core controller accepts either for a track playlist. Requiring
        # the new flag on its own would reject destinations core allows.
        if not any(feature in provider.supported_features for feature in PLAYLIST_CREATE_FEATURES):
            msg = f"{provider.name} does not support creating playlists"
            raise InvalidDataError(msg)
        # mirrors upstream #5989: creating a playlist is not enough, the
        # destination must also support editing it afterwards to add tracks.
        if ProviderFeature.PLAYLIST_TRACKS_EDIT not in provider.supported_features:
            msg = f"{provider.name} does not support editing playlists"
            raise InvalidDataError(msg)
        # mirrors upstream #5989: some providers can create playlists but
        # cannot actually serve track-type media (e.g. podcast/audiobook-only
        # providers), so reject those explicitly rather than fail deep inside
        # the migration task.
        if MediaType.TRACK not in provider.supported_media_types:
            msg = f"{provider.name} does not support track playlists"
            raise InvalidDataError(msg)
        return provider

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
        :param match_policy: ACCEPTED FOR FRONTEND REQUEST-SHAPE COMPATIBILITY
            ONLY. IT HAS NO EFFECT IN THIS BRIDGE. Upstream's own
            ``migrate_playlist`` (server PR #5989, shipping in server
            2.11.0) implements this for real as a typed
            ``PlaylistMatchPolicy`` defaulting to ``SAME_RECORDING``. The
            already-merged matching pipeline this plugin calls into
            (``import_playlist`` / ``match_imported_playlist_tracks``)
            exposes no confidence knob, so every value passed here currently
            produces the same best-effort metadata match regardless of what
            the caller asked for. Do not treat this parameter as honoured;
            a real policy distinction only exists once the server pin
            reaches 2.11.0, at which point this bridge is retired (see
            docs/playlist-bridge-vs-upstream.md). Disabling the policy
            selector in the frontend UI is a separate change in a different
            repository, not something this plugin can or should fake.
        :param name: Optional name for the new playlist; defaults to the
            source playlist's name.
        """
        # resolve and validate before scheduling any work
        destination = self._resolve_destination(destination_provider)
        playlist = await self.mass.music.playlists.get_library_item(int(db_playlist_id))
        # mirrors upstream #5989: a dynamic (smart) playlist has no fixed
        # track list to export, so migrating it does not make sense.
        if playlist.is_dynamic:
            msg = "Dynamic playlists can not be migrated"
            raise InvalidDataError(msg)
        # mirrors upstream #5989's intent (there enforced via
        # allowed_provider_instances / get_current_user): the SOURCE
        # playlist's own provider must also be one of the caller's
        # available, configured providers, not merely the destination.
        # self.mass.music.providers is already scoped to the caller for
        # this fork's purposes (see _resolve_destination above); "builtin"
        # is always allowed since library playlists live there regardless
        # of provider scope.
        available_instance_ids = {p.instance_id for p in self.mass.music.providers if p.available}
        if not any(
            mapping.provider_domain == "builtin" or mapping.provider_instance in available_instance_ids
            for mapping in playlist.provider_mappings
        ):
            msg = f"{playlist.name} is not available from one of your configured providers"
            raise ProviderUnavailableError(msg)
        destination_name = name or playlist.name
        # mirrors upstream #5989: validate the destination playlist name the
        # same way core's own create_playlist path does, rejecting path
        # separators and traversal components.
        if not is_safe_name(destination_name):
            msg = f"{destination_name} is not a valid Playlist name"
            raise InvalidDataError(msg)
        del match_policy  # accepted, not honoured; see docstring above
        return self.mass.tasks.run_background_task(
            name=f"Migrate playlist {playlist.name} to {destination.name}",
            handler=lambda: self._migrate_playlist(playlist, destination, destination_name),
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
        self, playlist: Playlist, destination: MusicProvider, destination_name: str
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
        try:
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
                destination_name, [MediaType.TRACK], destination.instance_id
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
        finally:
            # matched_playlist is a throwaway builtin copy that exists only to run
            # the matching pipeline; without this it leaks an orphaned .m3u file
            # in the builtin provider's playlists directory on every call. Runs
            # whether the migration above succeeded or raised. A cleanup failure
            # here is logged, not raised, so it never converts a successful
            # migration into a failure and never masks a real migration error.
            try:
                await builtin.library_remove(matched_playlist.item_id, MediaType.PLAYLIST)
            except Exception as err:  # noqa: BLE001
                self.logger.warning(
                    "Failed to remove throwaway playlist %s: %s", matched_playlist.item_id, err
                )

    async def archive_playlists(self, source_provider: str | None = None) -> BackgroundTask:
        """
        Queue copying many library playlists into the builtin provider as local .m3u files.

        This exists so a library built up on a streaming provider (with no
        local files at all) can still have a durable, offline copy of its
        playlist structure that survives independently of that provider.

        :param source_provider: Instance id or domain of a provider to
            archive from (e.g. "spotify"). None archives every eligible
            library playlist regardless of provider.

        Idempotent and resumable by design: a playlist is skipped when a
        builtin playlist with its exact name already exists, so re-running
        this command (after a partial run, a restart, or just to pick up
        newly added playlists) only archives what is not already archived.
        It does not detect renames, so renaming either the source or the
        archived copy breaks that link and the next run archives it again
        under the new name.

        Matching against other providers is deliberately left off
        (``import_playlist(..., library_matching=False)``): with hundreds of
        playlists this would otherwise trigger tens of thousands of provider
        searches for no benefit, since the goal here is a local copy of the
        playlist, not a cross-provider match. Use ``migrate_playlist`` when a
        matched copy on a specific destination provider is actually wanted.
        """
        return self.mass.tasks.run_background_task(
            name=f"Archive playlists ({source_provider or 'all providers'})",
            handler=lambda: self._archive_playlists(source_provider),
            task_id=f"playlist_bridge_archive_{source_provider or 'all'}",
            translation_key="playlist_bridge_archive",
            translation_owner=self.translation_owner,
            translation_args=[source_provider or "all providers"],
            metadata={
                "task_domain": "playlist_bridge_archive",
                "source_provider": source_provider or "",
            },
            allow_retry=True,
        )

    async def _archive_playlists(self, source_provider: str | None) -> None:
        """Copy eligible library playlists into the builtin provider, one at a time."""
        # a single enumeration serves both the existing-names check and the
        # progress total, so a large library is only scanned once
        playlists = [item async for item in self.mass.music.playlists.iter_library_items()]
        total = len(playlists)
        existing_builtin_names = {
            item.name
            for item in playlists
            if any(mapping.provider_domain == "builtin" for mapping in item.provider_mappings)
        }

        archived = 0
        failed: list[str] = []
        skipped_dynamic = 0
        skipped_builtin_only = 0
        skipped_provider_mismatch = 0
        skipped_already_archived = 0

        for index, playlist in enumerate(playlists):
            # a dynamic (smart) playlist has no fixed track list to export
            if playlist.is_dynamic:
                skipped_dynamic += 1
                continue
            # never archive an archive
            if all(mapping.provider_domain == "builtin" for mapping in playlist.provider_mappings):
                skipped_builtin_only += 1
                continue
            if source_provider is not None and not any(
                mapping.provider_domain == source_provider or mapping.provider_instance == source_provider
                for mapping in playlist.provider_mappings
            ):
                skipped_provider_mismatch += 1
                continue
            # the idempotency/resumability rule: a prior run already archived this name
            if playlist.name in existing_builtin_names:
                skipped_already_archived += 1
                continue

            update_current_task_progress_from_index(index, total, f"Archiving {playlist.name}")
            try:
                m3u_data = await self.mass.music.playlists.export_playlist(playlist.item_id)
                # the controller's import_playlist, not the provider's: only the
                # controller also adds the result to the library so it shows in
                # the UI. library_matching=False on purpose, see the docstring
                # on archive_playlists above.
                await self.mass.music.playlists.import_playlist(m3u_data, library_matching=False)
            except MusicAssistantError as err:
                # one bad playlist must never abort the run
                report_current_task_failure(f"{playlist.name}: {err}")
                failed.append(playlist.name)
                continue
            archived += 1

        report_lines = [
            "# Playlist archive summary",
            f"- Archived: {archived}",
            f"- Skipped (dynamic): {skipped_dynamic}",
            f"- Skipped (builtin-only): {skipped_builtin_only}",
            f"- Skipped (provider mismatch): {skipped_provider_mismatch}",
            f"- Skipped (already archived): {skipped_already_archived}",
            f"- Failed: {len(failed)}",
        ]
        if failed:
            report_lines.append("")
            report_lines.append("Failed playlists:")
            report_lines.extend(f"- {name}" for name in failed)
        set_current_task_report("\\n".join(report_lines))
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
