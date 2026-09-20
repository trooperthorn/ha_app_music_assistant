# Changelog

## 2026-09-20

- Legacy iTunes inspection accepts a safely staged ZIP package, including a
  direct authenticated XML-only ZIP upload for compact transport, then maps the
  archived paths against media already known to Music Assistant.
- Fork frontend v2026.09.20.6 -> v2026.09.20.7
- Library Enrichment can safely inspect a staged legacy iTunes XML export and
  persist a digest-bound, revisioned playlist/path-mapping preview without
  changing the Music Assistant library.
- Fork frontend v2026.09.20.5 -> v2026.09.20.6
- Library Enrichment adds API-v1 read-only typed Spotify provenance from immutable
  captures and links builtin playlist destinations to archive/checkpoint state,
  without provider refreshes or inline raw payloads.
- Fork frontend v2026.09.20.4 -> v2026.09.20.5
- Library Enrichment adds revisioned per-subscription playback policies and an
  explicit preview/apply workflow for durable builtin playback projections,
  including approved-local selection, visible fallback gaps, and fail-closed
  `local_only` provider signaling.
- Fork frontend v2026.09.20.3 -> v2026.09.20.4
- Library Enrichment can review existing merged library-backed mappings as
  local-match candidates and retain revisioned approvals and rejections without
  changing Music Assistant mappings or playback routing.
- Fork frontend v2026.09.20.2 -> v2026.09.20.3
- Library Enrichment subscriptions support explicit revisioned manual or
  scheduled snapshot checks, durable sync jobs and access state, unchanged-source
  short-circuiting, and immutable capture when Spotify reports a new snapshot.
- Fork frontend v2026.09.20.1 -> v2026.09.20.2
- Library Enrichment can explicitly project a committed archive version into a
  visible builtin playlist with omission review, ordered-reference verification,
  durable idempotency and separate applied checkpoints.
- Fork frontend v2026.09.19.5 -> v2026.09.20.1

## 2026-09-19

- Fork frontend v2026.09.19.4 -> v2026.09.19.5
- Fork frontend v2026.09.19.2 -> v2026.09.19.4
- Fork frontend v2026.09.19.1 -> v2026.09.19.2
- Music Assistant server 2.10.3 -> 2.10.4
- Fork frontend v2026.09.16.2 -> v2026.09.19.1

## 2026-09-17

- Fork frontend v2026.09.16.1 -> v2026.09.16.2

## 2026-09-16

- Fork frontend v2026.09.15.1 -> v2026.09.16.1

## 2026-09-15

- Fork frontend v2026.09.14.9 -> v2026.09.15.1
- Fork frontend v2026.09.14.8 -> v2026.09.14.9
- Upstream app config.yaml refreshed
- Upstream app translations/en.yaml refreshed
- Fork frontend v2026.09.14.7 -> v2026.09.14.8

## 2026-09-14

- Music drive: an app option mounts a partition of the server inside this app at /music/<label> (exFAT, FAT, NTFS, ext4, btrfs, xfs), read-only when an exFAT volume was not cleanly ejected, with one-off backup, verify, restore and repair tasks under /share
- browse_path: a config/providers/browse_path command for the fork frontend's folder picker in the Filesystem provider setup
- music_trash: music/trash/move, list, restore and empty commands so the fork frontend's Duplicates page can move a Filesystem provider's file into .music-assistant-trash/ on the same drive and bring it back
- Fork frontend v2026.09.14.5 -> v2026.09.14.7
- Fork frontend v2026.09.14.3 -> v2026.09.14.5
- Fork frontend v2026.09.14.2 -> v2026.09.14.3
- Fork frontend v2026.09.14.1 -> v2026.09.14.2
- Fork frontend v2026.09.13.14 -> v2026.09.14.1
- Fork frontend v2026.09.13.13 -> v2026.09.13.14
- Fork frontend v2026.09.13.2 -> v2026.09.13.13

## 2026-09-13

- Fork frontend v2026.09.13.1 -> v2026.09.13.2
- Fork frontend v2026.09.12.5 -> v2026.09.13.1

## 2026-09-12

- Fork frontend v2026.09.12.4 -> v2026.09.12.5
- Fork frontend v2026.09.12.2 -> v2026.09.12.4
- Fork frontend none -> v2026.09.12.2
- First definition: upstream server 2.10.3; the fork frontend is pinned by the first upstream sync once its wheel is published.
