# Selected playlist refresh

Library Enrichment supports explicit manual refresh and opt-in scheduled capture
for an existing source subscription. It does not automatically create, replace,
or edit a builtin playlist copy when a new source version arrives.

`library_enrichment/sync_policy` reads a subscription policy.
`set_sync_policy(subscription_id, expected_revision, mode, interval_seconds)`
changes it using optimistic revision checking. Modes are `manual` and `scheduled`;
intervals range from 3,600 through 604,800 seconds. Saving a scheduled policy
records the configuring user, whose account, current permissions, and provider
visibility are checked again when the job runs.

One Music Assistant hourly task checks at most ten due subscriptions sequentially.
An interval is a minimum spacing, not an exact launch time: dispatch granularity,
the MA task queue, and provider throttling can delay work. Switching to manual
stops future automatic checks, while an already active job may finish.

`sync_now(subscription_id)` returns `job_id`, `task_id`, and state `queued` and
queues priority work. `sync_status(subscription_id)` returns `policy`, operational
`state`, chronological `jobs`, and `latest_job`. Job states are `queued`, `running`,
`succeeded`, `failed`, `cancelled`, and `interrupted`. Core-task cancellation is
reconciled when sync status is read. A provider restart interrupts unfinished jobs
and keeps the last complete archive.

Each check reads fresh Spotify metadata through the existing provider session.
If the authenticated account or authorization changes, the check cannot commit
under the old identity. An unchanged committed snapshot skips all item pages and
links the existing immutable version. A changed snapshot captures all occurrences,
checks the final snapshot, and commits atomically. Manual captures and refreshes
cannot simultaneously own the same subscription.

Authentication and access failures pause automatic checks until policy is saved
again or a manual check succeeds. Other failures retain the previous complete
version and use the normal interval. Spotify's existing throttler owns HTTP retry
and Retry-After handling; no independent immediate outer retry is added. Its final
`RetriesExhausted` exception does not expose a reliable rate-limit deadline, so the
extension does not claim precise persisted Retry-After scheduling.

This implementation is pinned to Music Assistant 2.10.4. Offline tests cover the
unchanged fast path, changed captures, account and actor isolation, failures,
bounded dispatch, exclusion, cancellation, restart, and absence of automatic apply.
These tests do not establish successful execution against a live Spotify account.
