# Library Enrichment provenance API v1

`library_enrichment/provenance` is an explicit, administrator-scoped read of one
committed archive version. It parses only the immutable `source_payload` already
stored for that version. It performs no Spotify request, Music Assistant lookup,
metadata refresh, or playlist mutation.

The response is occurrence-aligned and paged with `limit` 1 through 200 and a
nonnegative `offset`. Repeated Spotify track IDs retain separate occurrence rows
but resolve to the same subject within the source account. Subjects from different
accounts remain separate. Raw payload JSON is never returned. Each observation
records its archive version, occurrence position, JSON pointer, parser version,
source, and the version capture timestamp.

API v1 extracts `spotify_track_id`, `spotify_album_id`, `spotify_artist_ids`,
`isrc`, `duration_ms`, `explicit`, `popularity`, `added_at`, `added_by_id`,
`is_local`, and `album_release_date`. Duration carries unit `ms`. Release dates
carry Spotify's declared `year`, `month`, or `day` precision, with shape-based
precision used when that declaration is absent.

Every field envelope has one of five states:

- `value`: a value of the expected type was archived.
- `empty`: an explicit empty string or list was archived.
- `missing`: the containing object was loaded but omitted the field.
- `not_loaded`: the occurrence or explicit field value was null.
- `inaccessible`: the containing structure or value had an unusable shape.

The persisted field overlay contains its current observation, optional revisioned
override, and effective value. This slice does not add an override write command.

`library_enrichment/item_provenance` accepts `media_type: playlist` and a nonempty
Music Assistant library item ID. For a known builtin archive-copy or playback
destination, it returns `linked: true`, destination kind and version, source
subscription identity, observed/attempted/committed snapshot IDs and timestamps,
and durable check timestamps/access state. Its state is one of `current`,
`source_changed`, `capture_pending`, `capture_failed`, or `unknown`. An unlinked
playlist returns `linked: false`, `state: unknown`, and null linkage sections.
Timestamps are persisted UTC ISO-8601 values; the read invents no observation time.

Clients must check both capability booleans and API versions. The server advertises
`raw_payload_inline: false`; clients fail closed on an unknown state or API version.
