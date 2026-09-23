# Library Enrichment provenance API v1

`library_enrichment/provenance` is an explicit, administrator-scoped read of one
committed archive version. Spotify fields are parsed only from the immutable
`source_payload` stored for that version. It performs no Spotify request,
metadata refresh, provider search, remote MusicBrainz request, or playlist mutation.
When the identity capability is present, it also performs the bounded local Music
Assistant mapping lookup described below.

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

When the archived occurrence has a Spotify track ID, the same read also asks the
Music Assistant track controller for an existing library mapping for that Spotify
provider instance. This is a local database lookup: it does not call the normal
refresh-on-access method, search a provider, or contact MusicBrainz. The resulting
snapshot is appended to the existing provenance subject with source
`music_assistant.library` and parser version `ma-library-identity-v1`.

The identity fields keep MusicBrainz entity types separate:

- `musicbrainz_recording_id` is the recording identity.
- `musicbrainz_release_track_id` is the track position on a specific release.
- `musicbrainz_release_id` is the release identity.
- `musicbrainz_release_group_id` is the release-group identity.
- `musicbrainz_artist_credits` is an ordered list. Each entry includes its
  zero-based position, credited name, artist ID, and an explicit identity state.

The same local lookup also snapshots `ma_label`, `ma_album_barcode`,
`ma_artwork_sources`, and `ma_audio_formats` when the corresponding MA data is
available. Artwork observations contain only type, provider, and proxy ID;
provider mapping item IDs and raw artwork paths are excluded. Audio formats
contain provider domain, content type, sample rate, bit depth, channels, and bit
rate. The source remains `music_assistant.library`; no remote metadata lookup is
performed. MA's track model does not expose a general catalog-number field, so
this API does not invent one. A failed local read marks prior catalog values
stale and retries on the next read.

No field substitutes one MusicBrainz ID type for another. A missing library match
is `not_loaded`. A failed local database read records `stale`; when an earlier
successful identity observation exists, its value remains visible with that stale
state instead of being replaced by an error. Without earlier usable evidence, the
field is `inaccessible`. Read-error observations do not complete the version-bound
snapshot, so a later provenance read retries the local lookup. Loaded records continue to distinguish
`missing`, `empty`, and malformed `inaccessible` data.

Only unique source IDs represented on the requested page are looked up. The first
identity result for an archive version is bound to that version's occurrence and
reused on later reads, so a newer version or a later change in the MA library cannot
rewrite the older version's response. The observation time records when that first
version-specific provenance read occurred.

Every field envelope has one of five states:

- `value`: a value of the expected type was archived.
- `empty`: an explicit empty string or list was archived.
- `missing`: the containing object was loaded but omitted the field.
- `not_loaded`: the occurrence or explicit field value was null.
- `inaccessible`: the containing structure or value had an unusable shape.

The persisted field overlay contains its current observation, optional revisioned
override, and effective value. Administrators with provider configuration permission
can set or clear a correction for an observed field by supplying the archive version,
source track ID, field name, and expected revision. The provider verifies that the
track belongs to that version and keeps the original observation; a conflicting
revision is rejected so the client must refresh before another edit.

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
Clients must additionally require `musicbrainz_identity_api_version: 1` before
depending on the additive Music Assistant identity fields.
`local_catalog_api_version: 1` advertises the additive local catalog fields.
