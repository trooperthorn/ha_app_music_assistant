# Local match review contract

This first Phase 4 slice records reviewable evidence from mappings Music
Assistant has already merged. It does not infer an exact edition, mutate the MA
library, or change playback routing.

`library_enrichment/match_review` accepts an immutable archive `version_id` and
a bounded occurrence page (`limit` 1 through 200 and a nonnegative `offset`).
It returns the original occurrence position/state/source ID plus an
account-scoped match overlay. Repeated occurrences remain visible separately but
share the same source overlay and revision. The source key is Spotify provider
domain, authenticated account ID, media type and Spotify item ID; playlist name
and MA integer IDs are not identity.

Candidates are generated only from an existing merged MA track record. Each
candidate is an available non-streaming provider mapping and includes exact
provider domain, provider instance, provider item ID and the MA library item ID
as evidence. A single candidate is still only a candidate. Multiple candidates
are reported as ambiguous. Missing merged records remain unmatched. Streaming,
builtin and unavailable provider mappings are excluded.

The response includes `candidate_freshness` (`fresh` or `stale`) and a bounded
`candidate_error`. If a direct library read fails and a prior overlay exists,
the prior candidates remain available as stale. The command performs no retry,
provider search, enrichment or refresh-on-access lookup.

`library_enrichment/set_match_decision` accepts `version_id`, `source_item_id`,
`expected_revision`, `action` and an `asset_id` for approval or rejection.
`approve` and `reject` require a current candidate. Rejection is candidate
specific, so remaining candidates can stay available for review. `clear`
removes the active decision state. The compare-and-swap revision prevents two review screens
from silently overwriting each other. Decisions store the actor, evidence and
algorithm version and are isolated by Spotify account.

Both commands require an authenticated user with `config.providers.write`.
Changing a decision additionally requires `library.write`. Neither command
changes provider mappings. A separate explicit playback projection can use an
approved local asset; `local_only` requests are enforced in the final stream
selector and buffer acquisition, including cached details and capacity retries.
These code paths have fixture tests against the pinned server but have not yet
been acoustically validated on the installed players.

The store can relocate an existing asset's provider location atomically after a
reviewed move, retaining its asset ID and decision history while rejecting a
stale old path or a destination already bound to another asset. This is a
store-level primitive; the provider API and UI still need a live mapping check
and explicit correction workflow before users can invoke it.
