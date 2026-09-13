# Upstream review: method and measurements

How [upstream-review.md](upstream-review.md) and the fork's fuller report were
measured, so both can be re-run and checked rather than trusted. Everything
below was taken on **2026-09-13**.

Version comparisons go stale. Re-run them before acting on any of it.

## Trees the review was taken against

| Tree | Commit |
| --- | --- |
| `trooperthorn/HA_int_MA-UI` | `d8784ac` |
| `music-assistant/frontend` `main` | `fdabf43` |
| `music-assistant/server` | tag `2.10.3` and `main` |
| `trooperthorn/ha_app_music_assistant` | `43b7d84` |

The server was read at **both** `2.10.3` and `main`, deliberately. They differ
in ways that change the answer — most importantly ffmpeg, which is 7.1.2 in
the shipped tag and 9.0.1 on `main`. Reading only `main` overstates how
current the shipped image is.

## Fork against upstream frontend

```sh
git -C HA_int_MA-UI fetch ../frontend main:upstream_main
MB=$(git merge-base HEAD upstream_main)          # ce95e28, 2026-09-12
git rev-list --count $MB..upstream_main          # 23  (behind)
git rev-list --count $MB..HEAD                   # 46  (ahead)
git diff --shortstat $MB..HEAD                   # 75 files, +14409 -533
comm -12 <(git diff --name-only $MB..HEAD | sort) \
         <(git diff --name-only $MB..upstream_main | sort) | wc -l   # 13
```

The 13 overlapping files are the merge-conflict surface, and the 23 upstream
commits are the onboarding and user-roles epic. That is the fork's finding,
not this repository's; it is recorded here only because the numbers above are
what produced it.

Also checked: `git diff $MB..HEAD -- package.json` is **empty**. The fork
changes no dependencies, so npm dependency currency is entirely an upstream
question.

## Dependency currency

Queried the registries directly rather than relying on a lockfile:

- npm: `https://registry.npmjs.org/<pkg>`, reading `dist-tags.latest` and the
  matching `time` entry for its publish date.
- PyPI: `https://pypi.org/pypi/<pkg>/json`, reading `info.version` and the
  upload time of that release.
- Native components: the upstream projects' own release and tag listings.

### npm — stale or unmaintained

| Package | Pinned | Last publish |
| --- | --- | --- |
| `butterchurn-presets` | 2.4.7 | 2018-06-09 |
| `butterchurn` | GitHub tarball URL | n/a — not in any registry |
| `mobile-detect` | 1.4.5 | 2021-03-13 |
| `material-design-icons-iconfont` | 6.7.0 | 2022-04-23 |
| `@mdi/font` | 7.4.47 | 2023-12-27 |

### npm — major versions behind

`vuetify` 3.12.0 → 4.2.1 · `@tanstack/vue-table` 8 → 9 · `typescript` 6 → 7 ·
`vitest` / `@vitest/ui` / `@vitest/coverage-v8` 4 → 5.

Also: `happy-dom`, a test DOM, is declared in `dependencies` rather than
`devDependencies`. Everything else is current or one patch behind, including
`dompurify` (3.4.14 against 3.4.15).

### Python, at server tag 2.10.3

Exactly latest: `aiohttp` 3.14.3, `aiosqlite` 0.22.1, `mutagen` 1.48.1,
`pillow` 12.3.0.

Minor lag: `cryptography` 50.0.0/50.0.1 · `orjson` 3.11.6/3.12.0 ·
`zeroconf` 0.149.16/0.151.3 · `mashumaro` 3.20/3.22.

Major behind: `librosa` 0.11.0 against 1.0.0.

Deliberately held: `numpy` 2.3.5 against 2.5.3, with the reason inline —
numpy 2.4.0+ raises the x86 baseline to SSE4.2 and breaks older CPUs. This is
the kind of pin an automated dependency bot gets wrong; it should not be
"fixed".

Separately worth noting: a full `torch` / `torchaudio` / `librosa` stack ships
inside the image. That is a size and aarch64 cost independent of currency.

### Native components

Read from `Dockerfile.base` at each ref, not from `main` alone:

```sh
git show 2.10.3:Dockerfile.base | grep -E 'FFMPEG_VERSION|SHAIRPORT_VERSION|SNAPCAST_VERSION|GO_LIBRESPOT_VERSION|PYTHON_VERSION'
```

The `aiolibdatachannel` branch pin was confirmed live:

```sh
git ls-remote https://github.com/music-assistant/aiolibdatachannel
# refs/heads/feat/certpem-testdrive  950e7f0   (the pin)
# refs/heads/main                    a7f2683   (ahead of it)
# refs/tags/2026.9.5                 46ce109   (proper releases exist)
```

## Backlog coverage, and its limit

**92 of 98 open issues were confirmed.** The GitHub API was not reachable from
the environment the review was written in, so issues were enumerated by paging
the web UI, which returns partial pages. The triage in the fork's report is
therefore near-complete, not complete.

Confirmed: #2, 3, 4, 6–21, 23–27, 29–34, 37, 39, 41, 43–48, 51–54, 56, 58–64,
66, 68–73, 78, 79, 81–84, 91, 93–107, 110, 112, 115–123, 126–137.

The six unconfirmed fall among numbers that are mostly closed issues. Re-run
the triage with API access before treating that table as a census.

## Checked and dismissed

Recorded so nobody re-investigates them:

- **`sendspin-audio.com`** appears in the builtin sendspin provider, but only
  as a credits link in `manifest.json`. No runtime call.
- **`beta.music-assistant.io`** in the frontend's `helpers/utils.ts` rewrites
  *documentation* links on beta builds, and only when a user clicks one.
- **`public/sw.js`** is an HTTP-over-WebRTC proxy for remote mode, not a
  beacon.
- **SQL f-strings** in the server interpolate table and index names from
  `constants.py`, never values; values go through named bind parameters, and
  list parameters are expanded into placeholders rather than string-joined.
  There is no injection surface in the query layer.
- **Google Fonts** are downloaded at build time by `vite-plugin-webfont-dl`
  and self-hosted — the opposite of a runtime tracking call.
- **Backlog #51**, "fix security issues in unauthenticated endpoints", has an
  empty body and is marked Done on the project board. Low signal; not an
  identified open hole.

## What was not verified

Stated so the review is not over-read:

- **Individual CVE identifiers for shairport-sync.** The 4.3.7-to-5.5.1 gap
  and that the 5.x line carries security fixes are confirmed from the release
  listings. No specific advisory was matched to a version.
- **Runtime behaviour.** Nothing was executed: no image was built, no server
  was run, no library was loaded. Every finding is from reading source,
  manifests and build files.
- **The 100k-track claims are analytical**, derived from the paging code and
  SQLite's offset semantics, not from a benchmark against a real library.
