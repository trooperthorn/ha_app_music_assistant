# Changelog

## 2026-09-20

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
