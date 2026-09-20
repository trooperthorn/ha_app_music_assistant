"""Bounded, read-only inspection of legacy iTunes XML library exports."""

from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

MAX_BYTES = 128 * 1024 * 1024
MAX_ELEMENTS = 2_000_000
MAX_DEPTH = 32
MAX_TRACKS = 250_000
MAX_PLAYLISTS = 25_000
MAX_OCCURRENCES = 2_000_000
PARSER_VERSION = "itunes-xml-v1"
APPLE_PLIST_DOCTYPE = re.compile(
    rb'<!DOCTYPE\s+plist\s+PUBLIC\s+"-//Apple Computer//DTD PLIST 1\.0//EN"\s+'
    rb'"http://www\.apple\.com/DTDs/PropertyList-1\.0\.dtd"\s*>',
    re.IGNORECASE,
)


class ITunesXMLImportError(ValueError):
    """The supplied export cannot be inspected safely and completely."""


def _xml_value(element: ElementTree.Element, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ITunesXMLImportError("iTunes XML nesting exceeds the supported limit")
    tag = element.tag
    if tag == "dict":
        children = list(element)
        if len(children) % 2:
            raise ITunesXMLImportError("iTunes XML contains an incomplete dictionary")
        result = {}
        for index in range(0, len(children), 2):
            key = children[index]
            if key.tag != "key" or key.text is None:
                raise ITunesXMLImportError("iTunes XML dictionary contains an invalid key")
            if key.text in result:
                raise ITunesXMLImportError(f"iTunes XML contains duplicate key {key.text!r}")
            result[key.text] = _xml_value(children[index + 1], depth + 1)
        return result
    if tag == "array":
        return [_xml_value(child, depth + 1) for child in element]
    if tag in ("string", "key"):
        return element.text or ""
    if tag == "integer":
        try:
            return int(element.text or "")
        except ValueError as err:
            raise ITunesXMLImportError("iTunes XML contains an invalid integer") from err
    if tag == "real":
        try:
            return float(element.text or "")
        except ValueError as err:
            raise ITunesXMLImportError("iTunes XML contains an invalid real number") from err
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag == "date":
        value = element.text or ""
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as err:
            raise ITunesXMLImportError("iTunes XML contains an invalid date") from err
        return value
    if tag == "data":
        # Smart-playlist criteria are opaque plist data. Record only that bounded
        # data existed; never decode, return or persist its content.
        encoded = "".join((element.text or "").split())
        return {"omitted_plist_data": True, "encoded_length": len(encoded)}
    raise ITunesXMLImportError(f"Unsupported iTunes XML value type {tag!r}")


def _parse(data: bytes) -> dict:
    lowered = data.lower()
    if b"<!entity" in lowered:
        raise ITunesXMLImportError("DTD and entity declarations are not allowed")
    # Apple's XML export always declares its public plist DTD. Strip that exact,
    # inert declaration before ElementTree sees it; reject every other DTD form.
    data = APPLE_PLIST_DOCTYPE.sub(b"", data, count=1)
    if b"<!doctype" in data.lower():
        raise ITunesXMLImportError("DTD and entity declarations are not allowed")
    depth = elements = 0
    try:
        # DTD/entity declarations are rejected above and input/structure are bounded.
        for event, _ in ElementTree.iterparse(  # noqa: S314
            io.BytesIO(data), events=("start", "end")
        ):
            if event == "start":
                depth += 1
                elements += 1
                if depth > MAX_DEPTH or elements > MAX_ELEMENTS:
                    raise ITunesXMLImportError("iTunes XML exceeds structural limits")
            else:
                depth -= 1
        root = ElementTree.fromstring(data)  # noqa: S314 - guarded and bounded above
    except ElementTree.ParseError as err:
        raise ITunesXMLImportError("iTunes XML is malformed") from err
    if root.tag != "plist" or len(root) != 1 or root[0].tag != "dict":
        raise ITunesXMLImportError("Expected an XML property-list dictionary")
    value = _xml_value(root[0])
    if not isinstance(value, dict):
        raise ITunesXMLImportError("Expected an iTunes library dictionary")
    return value


def _identity(value, fallback: str) -> tuple[str, bool]:
    if isinstance(value, str) and value.strip():
        return value.strip(), False
    return fallback, True


def _location_path(location: object) -> str | None:
    if not isinstance(location, str) or not location:
        return None
    parsed = urlsplit(location)
    if parsed.scheme.lower() != "file":
        return None
    path = unquote(parsed.path).replace("\\", "/")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    elif re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return str(PurePosixPath(path))


def _root(path: str) -> str:
    if re.match(r"^[A-Za-z]:/", path):
        return path[0].upper() + path[1:3]
    if path.startswith("//"):
        parts = path.split("/")
        return "/".join(parts[:4]) + "/"
    return "/"


def _normalized_prefix(value: str) -> str:
    value = unquote(value).replace("\\", "/")
    if value.lower().startswith("file://"):
        parsed = _location_path(value)
        if parsed is None:
            raise ITunesXMLImportError("Path mapping source must be a file path or file URL")
        value = parsed
    value = str(PurePosixPath(value))
    return value.rstrip("/") + "/"


def _remap(path: str | None, mappings: list[dict]) -> dict:
    if path is None:
        return {"state": "no_location"}
    candidates = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ITunesXMLImportError("Path mappings must be objects")
        source = mapping.get("source_prefix")
        target = mapping.get("target_prefix", "")
        provider = mapping.get("provider_instance_id")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(provider, str) or not provider:
            raise ITunesXMLImportError("Path mapping requires source_prefix, target_prefix and provider_instance_id")
        prefix = _normalized_prefix(source)
        if path.casefold().startswith(prefix.casefold()):
            candidates.append((len(prefix), prefix, target.replace("\\", "/").rstrip("/"), provider))
    if not candidates:
        return {"state": "unmapped", "source_path": path}
    _, prefix, target, provider = max(candidates, key=lambda row: row[0])
    relative = path[len(prefix) :]
    if any(part == ".." for part in PurePosixPath(relative).parts):
        raise ITunesXMLImportError("Mapped path escapes its source root")
    item_id = "/".join(part for part in (target, relative) if part)
    return {
        "state": "mapped",
        "source_path": path,
        "source_prefix": prefix,
        "provider_instance_id": provider,
        "provider_item_id": item_id,
    }


def _playlist_classification(playlist: dict) -> tuple[str, bool]:
    if playlist.get("Folder") is True:
        return "folder", False
    if playlist.get("Master") is True or "Distinguished Kind" in playlist:
        return "system", False
    if "Smart Info" in playlist or "Smart Criteria" in playlist:
        return "smart_snapshot", True
    if not isinstance(playlist.get("Playlist Items"), list):
        return "empty", True
    return "user", True


def inspect_itunes_xml(
    path: str | Path,
    *,
    path_mappings: list[dict] | None = None,
    max_bytes: int = MAX_BYTES,
) -> dict:
    """Return a deterministic preview without modifying iTunes or Music Assistant."""
    source = Path(path)
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ITunesXMLImportError(f"Maximum size must be between 1 and {MAX_BYTES} bytes")
    try:
        size = source.stat().st_size
    except OSError as err:
        raise ITunesXMLImportError("iTunes XML is not readable") from err
    if size > max_bytes:
        raise ITunesXMLImportError("iTunes XML exceeds the configured size limit")
    try:
        data = source.read_bytes()
    except OSError as err:
        raise ITunesXMLImportError("iTunes XML is not readable") from err
    if len(data) != size:
        raise ITunesXMLImportError("iTunes XML changed while it was being read")
    digest = hashlib.sha256(data).hexdigest()
    library = _parse(data)
    tracks_raw = library.get("Tracks", {})
    playlists_raw = library.get("Playlists", [])
    if not isinstance(tracks_raw, dict) or not isinstance(playlists_raw, list):
        raise ITunesXMLImportError("iTunes XML has invalid Tracks or Playlists containers")
    if len(tracks_raw) > MAX_TRACKS or len(playlists_raw) > MAX_PLAYLISTS:
        raise ITunesXMLImportError("iTunes XML exceeds track or playlist limits")
    library_id, library_id_fallback = _identity(library.get("Library Persistent ID"), f"sha256:{digest}")
    mappings = path_mappings or []
    if not isinstance(mappings, list):
        raise ITunesXMLImportError("Path mappings must be a list")

    tracks = {}
    roots: dict[str, int] = {}
    for dictionary_id, raw in tracks_raw.items():
        if not isinstance(raw, dict):
            raise ITunesXMLImportError("iTunes track records must be dictionaries")
        track_id, weak = _identity(raw.get("Persistent ID"), f"track-id:{dictionary_id}")
        location = raw.get("Location")
        path_value = _location_path(location)
        if path_value is not None:
            roots[_root(path_value)] = roots.get(_root(path_value), 0) + 1
        media = str(raw.get("Kind", "")).casefold()
        unsupported = any(token in media for token in ("video", "movie", "tv show", "podcast", "book"))
        tracks[str(dictionary_id)] = {
            "source_item_id": track_id,
            "identity_fallback": weak,
            "name": raw.get("Name"),
            "artist": raw.get("Artist"),
            "album": raw.get("Album"),
            "kind": raw.get("Kind"),
            "media_type": "unsupported" if unsupported else "track",
            "location": location if isinstance(location, str) else None,
            "path": _remap(path_value, mappings),
        }

    occurrence_total = 0
    mapping_counts = {"matched": 0, "unresolved": 0, "ambiguous": 0, "unsupported": 0}
    warnings: list[dict] = []
    playlists = []
    for index, raw in enumerate(playlists_raw):
        if not isinstance(raw, dict):
            raise ITunesXMLImportError("iTunes playlist records must be dictionaries")
        classification, importable = _playlist_classification(raw)
        fallback_seed = raw.get("Playlist ID", f"{index}:{raw.get('Name', '')}")
        playlist_id, weak = _identity(raw.get("Playlist Persistent ID"), f"playlist-id:{fallback_seed}")
        members = raw.get("Playlist Items", [])
        if not isinstance(members, list):
            members = []
        occurrence_total += len(members)
        if occurrence_total > MAX_OCCURRENCES:
            raise ITunesXMLImportError("iTunes XML exceeds the playlist occurrence limit")
        occurrences = []
        seen: dict[str, int] = {}
        for position, member in enumerate(members):
            reference = member.get("Track ID") if isinstance(member, dict) else None
            track = tracks.get(str(reference))
            if track is None:
                occurrences.append({"position": position, "state": "missing_reference", "track_id": reference})
                mapping_counts["unresolved"] += 1
                if len(warnings) < 100:
                    warnings.append({"code": "missing_track_reference", "playlist_id": playlist_id, "position": position})
                continue
            source_item_id = track["source_item_id"]
            seen[source_item_id] = seen.get(source_item_id, 0) + 1
            occurrences.append(
                {
                    "position": position,
                    "state": track["media_type"],
                    "track_id": reference,
                    "source_item_id": source_item_id,
                    "path": track["path"],
                }
            )
            if track["media_type"] == "unsupported":
                mapping_counts["unsupported"] += 1
            elif track["path"]["state"] == "mapped":
                mapping_counts["matched"] += 1
            else:
                mapping_counts["unresolved"] += 1
        ordered = [f"{row['position']}:{row.get('state')}:{row.get('source_item_id')}:{row.get('track_id')}" for row in occurrences]
        snapshot = hashlib.sha256("\n".join(ordered).encode()).hexdigest()
        playlists.append(
            {
                "source_playlist_id": playlist_id,
                "id": playlist_id,
                "identity_fallback": weak,
                "name": raw.get("Name") if isinstance(raw.get("Name"), str) else "Untitled playlist",
                "parent_persistent_id": raw.get("Parent Persistent ID"),
                "parent_id": raw.get("Parent Persistent ID"),
                "classification": classification,
                "kind": classification,
                "importable": importable,
                "selectable": importable,
                "reason": None if importable else classification,
                "occurrence_count": len(occurrences),
                "track_count": len(occurrences),
                "duplicate_occurrence_count": sum(count - 1 for count in seen.values() if count > 1),
                "snapshot_id": snapshot,
                "occurrences": occurrences,
            }
        )
    return {
        "parser_version": PARSER_VERSION,
        "source_digest": digest,
        "source": {"path": str(source), "size": size, "sha256": digest},
        "library_id": library_id,
        "library_persistent_id": library_id,
        "library_identity_fallback": library_id_fallback,
        "library_date": library.get("Date"),
        "library_version": {
            "application": library.get("Application Version"),
            "major": library.get("Major Version"),
            "minor": library.get("Minor Version"),
        },
        "track_count": len(tracks),
        "tracks_total": len(tracks),
        "playlist_count": len(playlists),
        "playlists_total": len(playlists),
        "occurrence_count": occurrence_total,
        "path_roots": [
            {"root": root, "source_root": root, "track_count": count} for root, count in sorted(roots.items())
        ],
        "roots": [{"source_root": root, "track_count": count} for root, count in sorted(roots.items())],
        "mapping_summary": mapping_counts,
        "warnings": warnings,
        "warnings_truncated": len(warnings) == 100,
        "tracks": tracks,
        "playlists": playlists,
    }
