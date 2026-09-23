# Operations

## Workflows

| Workflow | Trigger | Does |
| --- | --- | --- |
| `test.yml` | push, PR, call | ruff, pytest on the scripts, YAML and version validation, a full image build that prints the server and frontend versions it serves |
| `validate.yml` | push, PR, weekly | app definition sanity, an upstream drift report (`sync_upstream.py --check`), hadolint |
| `security.yml` | push, PR, weekly | CodeQL and bandit on the scripts, an image vulnerability report (not a gate, see decisions.md) |
| `sync-upstream.yml` | daily 09:17 UTC, manual, and a `repository_dispatch` of type `fork-frontend-published` sent by the fork's `publish-fork.yml` right after it releases a wheel | applies the upstream pins, checks patch compatibility, then pushes `automation/upstream-sync` with the release App and opens an auto-merging PR only when the check passes |
| `release.yml` | push to main | runs tests and validate, then tags `v<version>` and publishes the GitHub release |
| `prepare-release.yml` | after a successful Release on main | when `music_assistant_lm/` changed since the last release and the version was not bumped, bumps CalVer on `automation/calver-release` and opens an auto-merging PR |

The chain after an upstream move: sync PR merges, Release runs (the version
is unchanged, so it publishes nothing new), Prepare release opens the CalVer
bump PR, that merges, Release publishes `v<new version>`, Home Assistant sees
the new `config.yaml` version and offers the update.

## Credentials

Both automation workflows mint a short-lived token from the release GitHub
App: repository variable `RELEASE_AUTOMATION_CLIENT_ID` and secret
`RELEASE_AUTOMATION_PRIVATE_KEY`. They fail before changing anything when
either is missing. The App is installed on this repository with contents
and pull-requests write.

## Manual sync

```bash
pip install -r requirements-test.txt
python scripts/sync_upstream.py --check   # report
python scripts/sync_upstream.py           # apply, then commit on a branch
```

`GH_TOKEN` raises the GitHub API rate limit but is not required.
Run `pytest -q tests/test_patches.py` after applying pins. If the server
version changed, review the anchored patches against that exact release and
refresh the pinned fixtures before publishing the sync branch. A failed
compatibility check stops the automated sync before it pushes a branch.

## Forcing a rebuild on the host

Home Assistant only rebuilds when `config.yaml`'s version changes. To ship
the same pins again (for example after fixing the Dockerfile), let Prepare
release bump the version, or run `python scripts/set_version.py
--next-from-tags` on a branch and merge it.

## Coexistence

Do not run this app and the official Music Assistant app at the same time:
both use `host_network` with port 8095 and register the same
`music_assistant` discovery service. Stop and uninstall the official one,
install this one, and the Home Assistant integration reconnects to the same
data directory contents only if you restore a backup of the official app
into this one (the `data` folders are separate per app).
