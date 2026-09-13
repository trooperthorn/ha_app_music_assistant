# Upstream review

What this app actually ships, what is and is not pinned, and the open upstream
items that affect it. Taken on 2026-09-13 against `music-assistant/server`
tag `2.10.3` (the pin in `music_assistant_lm/Dockerfile`) and its `main`.

This is the app-repository half of a wider review that also covers the
frontend fork, the library database and the 100k-track case. The full
document lives in the fork:

[HA_int_MA-UI `docs/UPSTREAM-BACKLOG-REVIEW.md`](https://github.com/trooperthorn/HA_int_MA-UI/blob/claude/upstream-backlog-comparison-7wuxbw/docs/UPSTREAM-BACKLOG-REVIEW.md)

The method and the raw measurements behind both are in
[upstream-review-notes.md](upstream-review-notes.md).

Nothing here has been acted on. These are findings and recommendations.

## What the app ships

`SERVER_VERSION` selects the whole runtime, including the audio stack. The
audio components are built into the upstream base image, not installed here,
and the versions in the shipped release differ from `main`:

| Component | Ships (server 2.10.3) | server `main` | Latest upstream |
| --- | --- | --- | --- |
| ffmpeg | 7.1.2 | 9.0.1 | 9.x |
| shairport-sync | 4.3.7 | 4.3.7 | 5.5.1 (2026-09-07) |
| snapcast | 0.34.0 | 0.34.0 | 0.35.0 (2026-03) |
| go-librespot | 0.9.0 | 0.9.0 | 0.9.1 (2026-09-07) |
| Python | 3.14 | 3.14 | 3.14 |

Two of these are worth knowing about:

- **shairport-sync 4.3.7** is the AirPlay *receiver*
  (`providers/airplay_receiver` runs one daemon per connected client), so it
  listens on the network on a `host_network` app. Upstream has moved to a 5.x
  line whose recent releases are security-motivated. The version gap and the
  security-motivated releases are confirmed; individual CVE identifiers were
  not checked, so treat this as "a major behind on a network-facing daemon"
  rather than a named vulnerability.
- **go-librespot 0.9.0** is backlog
  [#26](https://github.com/music-assistant/backlog/issues/26), "Update
  librespot", which cites two support tickets it may fix.

The base image builds ffmpeg from source with `--enable-libsoxr` and compiles
PyAV against those same libraries in the same image, so ffmpeg and PyAV
cannot drift apart. snapcast and go-librespot downloads are sha256-checked
upstream.

## Pinning `SERVER_VERSION` does not pin the audio stack

`docs/security.md` says the upstream image is "pulled by an exact version
tag". That is true and it is not enough:

- A tag is mutable. `FROM ghcr.io/music-assistant/server:${SERVER_VERSION}`
  resolves to whatever that tag points at when Supervisor builds, which is on
  the user's host, at install or update time — not at a moment this
  repository controls.
- Server `2.10.3`'s own Dockerfile uses `ARG BASE_IMAGE_VERSION=latest`. The
  base image carrying ffmpeg, shairport-sync and snapcast is not pinned by
  tag or digest at all, one layer down.

So the table above describes what 2.10.3 contained when it was built, not a
guarantee about what a rebuild produces.

The contrast is instructive, because this repository already does the right
thing one layer up. The fork wheel is pinned by release tag *and*
`FRONTEND_SHA256`, verified with `sha256sum --check` before `uv pip install`,
and the build fails closed on a mismatch.

**Recommendation.** Extend that discipline downward: have
`scripts/sync_upstream.py` resolve the server image digest and write it
alongside `SERVER_VERSION`, so the Dockerfile pins
`ghcr.io/music-assistant/server@sha256:...`. The script already rewrites
`SERVER_VERSION` (`sync_upstream.py:170`) and already computes a sha256 when
a release omits its sums file (`:94-96`), so the machinery is there. Cost: the
sync diff becomes slightly less readable, which the existing version comment
can absorb.

## A mutable dependency inside the shipped release

Server `pyproject.toml`, at tag `2.10.3`:

```python
# TEMPORARY test-drive pin — replace with PyPI release before merge
#   (see webrtc_aiolibdatachannel_plan.md Phase C)
"aiolibdatachannel @ git+https://github.com/music-assistant/aiolibdatachannel@feat/certpem-testdrive",
```

`git ls-remote` on that repository shows `feat/certpem-testdrive` is a live
branch at `950e7f0`, *behind* its own `main` (`a7f2683`), while the project
publishes tags up to `2026.9.5`.

A released image therefore depends on a feature branch that can be
force-pushed or deleted. Two builds of "2.10.3" are not guaranteed to be
identical, and the build can break without any version anywhere changing.

This is upstream's to fix — its own comment says exactly that — but this
repository inherits the consequence, and a digest-pinned base image would at
least make the failure visible rather than silent.

## AppArmor and privileges

`music_assistant_lm/apparmor.txt` grants bare `capability,`, `file,`,
`mount,`, `umount,`, `remount,`, wide network rules across every address
family, and `/dev/* mrwkl`. In practice it constrains little beyond signals.

It is inherited verbatim, and `scripts/sync_upstream.py:188` rewrites it from
`music-assistant/home-assistant-addon` on **every sync**. So a local
tightening is either overwritten on the next sync or becomes a permanent
divergence that has to be re-resolved each time. Raise it upstream rather
than patching it here; if a local profile is ever wanted, teach the sync
script to leave the file alone first, deliberately and as its own decision.

`SYS_ADMIN` and `DAC_READ_SEARCH` are load-bearing, not over-provisioning:
they exist for the `filesystem_smb` and `filesystem_nfs` mount providers.
`docs/security.md` lists them but does not say why, which makes them look
worse than they are in a review. Worth a sentence there.

## Open upstream items that land here

From the backlog, the ones that affect this repository rather than the
frontend:

| # | Title | Why it matters here |
| --- | --- | --- |
| [101](https://github.com/music-assistant/backlog/issues/101) | One-click updates, schedule that never interrupts playback | Changes how Music Assistant expects to be updated, which is this repository's job today |
| [102](https://github.com/music-assistant/backlog/issues/102) | HAOS and Supervisor: appliance mode, version source, variants | Touches the Supervisor contract this app builds against |
| [96](https://github.com/music-assistant/backlog/issues/96), [94](https://github.com/music-assistant/backlog/issues/94) | Music Assistant OS as a branded HAOS variant | **Strategic watch item.** A bundled OS image could change how app repositories like this one are distributed |
| [98](https://github.com/music-assistant/backlog/issues/98) | USB drives and NAS shares as music sources | Server-side, but it is what the `SYS_ADMIN` grant is for |
| [26](https://github.com/music-assistant/backlog/issues/26) | Update librespot | The `go-librespot 0.9.0` pin above |

## Call-home

No telemetry in this repository. The only external hosts are `github.com`,
`api.github.com` and `raw.githubusercontent.com`, in CI and
`scripts/sync_upstream.py`, at build and sync time.

The upstream server is a different matter — seven metadata providers are
builtin and reach external services by default, and the frontend makes one
always-on third-party call. Neither is telemetry, but both send data
outward. See the full report for the breakdown and what is opt-in.

## Recommendations

Ranked. The first two are this repository's to make.

1. **Digest-pin the server base image**, written by `sync_upstream.py`
   alongside `SERVER_VERSION`. Small, and it closes the gap between what
   `docs/security.md` claims and what a tag actually guarantees.
2. **Record why `SYS_ADMIN` and `DAC_READ_SEARCH` are needed** in
   `docs/security.md` — one sentence naming the SMB and NFS providers.
3. **Raise shairport-sync 4.3.7 upstream.** Network-facing, a major behind,
   security fixes in the intervening line.
4. **Raise the `aiolibdatachannel` branch pin upstream.** A shipped release
   should not depend on a mutable branch.
5. **Track #101 and #96.** Neither is work here yet; both could change what
   this repository is for.

Explicitly not recommended: tightening `apparmor.txt` locally. It trades a
finding for a permanent merge burden on every sync.
