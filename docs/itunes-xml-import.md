# Legacy iTunes XML import preview API v1

This integration safely inventories a legacy iTunes XML library, resolves mapped
paths against tracks already present in Music Assistant, and can explicitly create
one builtin playlist from one reviewed preview. It never imports or copies audio.

Place the XML export in the server-side import directory advertised by
`library_enrichment/capabilities`. A client Windows path such as `F:\...` is not
readable from a Home Assistant app container unless that storage is mounted or
the file is copied into the staging directory. Only `.xml` files whose resolved
paths remain inside that directory are accepted.

`library_enrichment/itunes_inspect` parses the file with bounded size, element,
depth, track, playlist and occurrence counts. It rejects DTD/entity declarations,
embedded plist data, duplicate dictionary keys, invalid dates and malformed XML.
Opaque plist data such as smart-playlist criteria is recognized but never decoded,
returned or persisted. The response contains the source SHA-256, library identity and version, aggregate
counts, path roots and compact playlist descriptors. Apple persistent IDs are
source identities; they are not ISRC or MusicBrainz identifiers.

User playlists and the resolved membership of smart playlists are selectable.
Folders, the master list and distinguished/system playlists are excluded with a
reason. Ordered occurrences and duplicates remain explicit. Missing Track-ID
references and unsupported media remain visible rather than being dropped.

`library_enrichment/itunes_preview` reparses the unchanged source and requires
explicit mappings with `source_prefix`, `target_prefix` and
`provider_instance_id`. Matching is case-insensitive for legacy Windows paths and
uses the longest source prefix. Traversal outside a mapped prefix is rejected.
Every mapping must name an available non-streaming provider instance visible to
the current administrator. A syntactically remapped path is only a candidate.
Preview calls Music Assistant's local `get_library_item_by_prov_id` lookup and
accepts a match only when the returned track still contains the exact provider
instance and item ID. It does not refresh metadata or search a provider.

The returned `preview_digest` binds the source bytes, mappings, selected playlist
IDs, ordered playlist snapshots, actual library track IDs and counts. Preview
state is revisioned and restart-safe, while raw XML is not stored in the database.

`library_enrichment/itunes_apply` requires exactly one selected playlist, the
reviewed source/preview digests and revision, the selected playlist ID, provider
configuration administration, and library-write permission. By default any
unresolved, ambiguous or unsupported occurrence fails closed. `allow_partial`
must be explicitly true to omit those visible gaps.

Apply reparses the unchanged source and revalidates every resolved library ID and
provider mapping before mutation. It creates one ordered M3U using
`library://track/<id>` entries with library matching disabled, preserving repeated
occurrences. It then reads the builtin M3U back and requires exact ordered content.
Schema v8 records durable intent before creation. A restart or failure after
creation begins becomes `uncertain` and is never automatically retried; committed
replays are idempotent. Use `library_enrichment/itunes_apply_status` to distinguish
`not_started`, `prepared`, `creating`, `applied`, `failed`, and `uncertain`.

## Staged ZIP packages

Capabilities advertise `itunes_zip_packages`, `itunes_zip_api_version: 1`,
`itunes_zip_upload`, its upload limit and the preferred
`/media/music-assistant-imports` staging directory. A ZIP placed there can contain
an XML library for comparison with media already indexed by Music Assistant.
Inspection reads the central directory, hashes the archive, identifies one
coherent iTunes XML export and returns a package inventory. It extracts no media.

ZIP inspection rejects absolute, drive-qualified and traversal member paths;
links and special files; encrypted entries; duplicate and case-colliding paths;
and packages that exceed entry, member, expanded-size or compression-ratio
limits. Multiple equally preferred XML exports require an explicit member path.
Path mappings remain `preview_only`; no destination directory, filesystem provider
or duplicate media file is created.

The authenticated `POST /library-enrichment/itunes-upload` route accepts a raw,
XML-only ZIP up to the advertised limit and stages it atomically under `/media`.
It rejects media members because the upload is only a compact transport for the
database export. Larger staged packages can still be copied into the advertised
directory with Home Assistant file tooling, but extraction is outside this slice.
