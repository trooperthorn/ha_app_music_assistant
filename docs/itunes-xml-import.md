# Legacy iTunes XML import preview API v1

This slice safely inventories a legacy iTunes XML library and persists an
explicit playlist/path-mapping preview. It does not create Music Assistant items
or playlists. Apply remains disabled until candidate verification and partial
import consent are implemented.

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
The returned `preview_digest` binds the source bytes, mappings, selected playlist
IDs, ordered playlist snapshots and counts. Preview state is revisioned and
restart-safe in schema v7, while raw XML is not stored in the enrichment database.

The next slice must verify mapped provider item IDs against available Music
Assistant filesystem providers, expose ambiguous/missing review, require explicit
partial consent and only then materialize selected playlists through supported
Music Assistant APIs.

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
