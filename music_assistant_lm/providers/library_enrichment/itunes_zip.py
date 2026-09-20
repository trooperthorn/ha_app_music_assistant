"""Safe, read-only inspection of staged iTunes ZIP packages."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .itunes_xml import MAX_BYTES as MAX_XML_BYTES
from .itunes_xml import ITunesXMLImportError, _parse

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_MEMBER_BYTES = 16 * 1024 * 1024 * 1024
MAX_XML_SCAN_BYTES = 512 * 1024 * 1024
SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {".aac", ".aif", ".aiff", ".alac", ".flac", ".m4a", ".m4b", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
)


class ITunesZipImportError(ValueError):
    """The staged ZIP cannot be inspected safely and completely."""


def _safe_member_path(name: str) -> str:
    """Return one canonical archive-relative path or reject it."""
    if not name or "\x00" in name:
        raise ITunesZipImportError("ZIP contains an empty or invalid member name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ITunesZipImportError(f"ZIP member uses an absolute path: {name!r}")
    path = PurePosixPath(normalized)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ITunesZipImportError(f"ZIP member path is unsafe: {name!r}")
    return str(path)


def _is_special_member(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    if not mode:
        return False
    kind = stat.S_IFMT(mode)
    return kind not in (0, stat.S_IFREG, stat.S_IFDIR)


def _read_member_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise ITunesZipImportError(f"ZIP member exceeds its inspection limit: {info.filename!r}")
    try:
        with archive.open(info, "r") as stream:
            data = stream.read(limit + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as err:
        raise ITunesZipImportError(f"ZIP member cannot be read: {info.filename!r}") from err
    if len(data) > limit or len(data) != info.file_size:
        raise ITunesZipImportError(f"ZIP member changed size while reading: {info.filename!r}")
    return data


def _xml_priority(path: str) -> int:
    name = PurePosixPath(path).name.casefold()
    if name == "itunes library.xml":
        return 0
    if name == "itunes music library.xml":
        return 1
    if "itunes" in name and name.endswith(".xml"):
        return 2
    return 3


def _find_xml_candidate(
    archive: zipfile.ZipFile,
    members: list[tuple[str, zipfile.ZipInfo]],
    requested_path: str | None,
) -> tuple[str, bytes, dict, int]:
    requested = _safe_member_path(requested_path) if requested_path is not None else None
    candidates: list[tuple[int, str, bytes, dict]] = []
    xml_scan_bytes = 0
    for path, info in members:
        if info.is_dir() or not path.casefold().endswith(".xml"):
            continue
        if requested is None and "itunes" not in PurePosixPath(path).name.casefold():
            continue
        if requested is not None and path != requested and "itunes" not in PurePosixPath(path).name.casefold():
            continue
        if info.file_size > MAX_XML_BYTES:
            if requested == path:
                raise ITunesZipImportError("Selected iTunes XML exceeds the supported size limit")
            continue
        xml_scan_bytes += info.file_size
        if xml_scan_bytes > MAX_XML_SCAN_BYTES:
            raise ITunesZipImportError("ZIP exceeds the iTunes XML inspection-byte limit")
        data = _read_member_bounded(archive, info, MAX_XML_BYTES)
        try:
            parsed = _parse(data)
        except ITunesXMLImportError:
            if requested == path:
                raise ITunesZipImportError("Selected member is not a valid iTunes XML library") from None
            continue
        if not isinstance(parsed.get("Tracks"), dict) or not isinstance(parsed.get("Playlists"), list):
            if requested == path:
                raise ITunesZipImportError("Selected XML does not contain an iTunes library")
            continue
        candidates.append((_xml_priority(path), path, data, parsed))
    all_candidates_total = len(candidates)
    if requested is not None:
        selected = [row for row in candidates if row[1] == requested]
        if selected:
            _, path, data, parsed = selected[0]
            return path, data, parsed, all_candidates_total
        raise ITunesZipImportError("Selected iTunes XML member was not found")
    if not candidates:
        raise ITunesZipImportError("ZIP does not contain an inspectable iTunes XML library")
    best_priority = min(row[0] for row in candidates)
    best = [row for row in candidates if row[0] == best_priority]
    if len(best) != 1:
        names = ", ".join(sorted(row[1] for row in best))
        raise ITunesZipImportError(f"ZIP contains ambiguous iTunes XML candidates: {names}")
    _, path, data, parsed = best[0]
    return path, data, parsed, all_candidates_total


def inspect_itunes_zip(
    path: str | Path,
    *,
    xml_member_path: str | None = None,
    localization_root: str = "iTunes Imported",
    max_entries: int = MAX_ENTRIES,
    max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: int = MAX_COMPRESSION_RATIO,
) -> dict:
    """Inspect a server-visible ZIP without extracting any archive member."""
    source = Path(path)
    if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
        raise ITunesZipImportError("Invalid ZIP entry limit")
    if type(max_uncompressed_bytes) is not int or not 1 <= max_uncompressed_bytes <= MAX_UNCOMPRESSED_BYTES:
        raise ITunesZipImportError("Invalid ZIP uncompressed-byte limit")
    if type(max_compression_ratio) is not int or not 1 <= max_compression_ratio <= MAX_COMPRESSION_RATIO:
        raise ITunesZipImportError("Invalid ZIP compression-ratio limit")
    root = _safe_member_path(localization_root)
    try:
        size = source.stat().st_size
    except OSError as err:
        raise ITunesZipImportError("Staged ZIP is not readable") from err
    if size > MAX_ARCHIVE_BYTES:
        raise ITunesZipImportError("Staged ZIP exceeds the archive size limit")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as err:
        raise ITunesZipImportError("Staged ZIP is not readable") from err
    try:
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as err:
        raise ITunesZipImportError("Staged file is not a valid ZIP archive") from err
    with archive:
        if len(infos) > max_entries:
            raise ITunesZipImportError("ZIP exceeds the configured entry limit")
        seen: set[str] = set()
        folded: set[str] = set()
        total = 0
        members: list[tuple[str, zipfile.ZipInfo]] = []
        manifest_rows: list[dict] = []
        media: list[dict] = []
        for info in infos:
            member_path = _safe_member_path(info.filename)
            if member_path in seen:
                raise ITunesZipImportError(f"ZIP contains a duplicate member: {member_path!r}")
            folded_path = member_path.casefold()
            if folded_path in folded:
                raise ITunesZipImportError(f"ZIP contains a case-colliding member: {member_path!r}")
            seen.add(member_path)
            folded.add(folded_path)
            if info.flag_bits & 0x1:
                raise ITunesZipImportError(f"ZIP contains an encrypted member: {member_path!r}")
            if _is_special_member(info):
                raise ITunesZipImportError(f"ZIP contains a link or special member: {member_path!r}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ITunesZipImportError(f"ZIP member exceeds the per-file size limit: {member_path!r}")
            total += info.file_size
            if total > max_uncompressed_bytes:
                raise ITunesZipImportError("ZIP exceeds the configured uncompressed-byte limit")
            if info.file_size and (not info.compress_size or info.file_size > info.compress_size * max_compression_ratio):
                raise ITunesZipImportError(f"ZIP member exceeds the compression-ratio limit: {member_path!r}")
            members.append((member_path, info))
            row = {
                "path": member_path,
                "size": info.file_size,
                "compressed_size": info.compress_size,
                "crc32": f"{info.CRC:08x}",
                "directory": info.is_dir(),
            }
            manifest_rows.append(row)
            suffix = PurePosixPath(member_path).suffix.casefold()
            if not info.is_dir() and suffix in SUPPORTED_MEDIA_EXTENSIONS:
                media.append(
                    {
                        "archive_path": member_path,
                        "size": info.file_size,
                        "media_format": suffix[1:],
                        "localization_path": f"{root}/{member_path}",
                    }
                )
        xml_path, xml_data, library, xml_candidates_total = _find_xml_candidate(archive, members, xml_member_path)
    manifest_rows.sort(key=lambda row: row["path"])
    media.sort(key=lambda row: row["archive_path"])
    manifest_payload = json.dumps(manifest_rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    files_total = sum(not row["directory"] for row in manifest_rows)
    media_bytes = sum(row["size"] for row in media)
    return {
        "source_kind": "zip",
        "archive": {"path": str(source), "size": size, "sha256": digest.hexdigest()},
        "package": {
            "entries_total": len(manifest_rows),
            "files_total": files_total,
            "media_files_total": len(media),
            "xml_candidates_total": xml_candidates_total,
            "compressed_bytes": sum(row["compressed_size"] for row in manifest_rows),
            "uncompressed_bytes": total,
            "selected_xml_path": xml_path,
            "warnings": [],
        },
        "localization": {
            "state": "preview_only",
            "proposed_root": root,
            "files_total": len(media),
            "bytes_total": media_bytes,
            "conflicts": 0,
        },
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "entry_count": len(manifest_rows),
        "uncompressed_bytes": total,
        "itunes_xml": {
            "archive_path": xml_path,
            "sha256": hashlib.sha256(xml_data).hexdigest(),
            "library_persistent_id": library.get("Library Persistent ID"),
            "track_count": len(library["Tracks"]),
            "playlist_count": len(library["Playlists"]),
        },
        "supported_media_count": len(media),
        "supported_media_bytes": media_bytes,
        "supported_media": media,
        "manifest": manifest_rows,
    }
