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
