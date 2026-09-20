"""Safety boundaries for staged iTunes ZIP packages."""

import importlib.util
import stat
import sys
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "music_assistant_lm.providers.library_enrichment"
for index, name in enumerate(("music_assistant_lm", "music_assistant_lm.providers", PACKAGE)):
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT.joinpath(*name.split(".")[index:]))]
    sys.modules.setdefault(name, package)

for module_name in ("itunes_xml", "itunes_zip"):
    full_name = f"{PACKAGE}.{module_name}"
    module_path = ROOT / "music_assistant_lm/providers/library_enrichment" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(full_name, module_path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = loaded
    spec.loader.exec_module(loaded)

module = sys.modules[f"{PACKAGE}.itunes_zip"]
ITunesZipImportError = module.ITunesZipImportError
inspect_itunes_zip = module.inspect_itunes_zip


XML = b"""<?xml version="1.0"?><plist version="1.0"><dict>
<key>Library Persistent ID</key><string>LIB1</string>
<key>Tracks</key><dict><key>1</key><dict><key>Name</key><string>Song</string></dict></dict>
<key>Playlists</key><array><dict><key>Name</key><string>Mix</string></dict></array>
</dict></plist>"""


def make_zip(tmp_path, rows):
    path = tmp_path / "library.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in rows:
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"target")
            else:
                archive.writestr(name, value)
    return path


def test_inventory_is_deterministic_and_does_not_extract(tmp_path):
    path = make_zip(
        tmp_path,
        [("old/iTunes Library.xml", XML), ("old/iTunes Media/Artist/Song.mp3", b"audio"), ("notes.txt", b"ignore")],
    )
    before = set(tmp_path.iterdir())
    first = inspect_itunes_zip(path)
    second = inspect_itunes_zip(path)
    assert set(tmp_path.iterdir()) == before
    assert first == second
    assert first["itunes_xml"] == {
        "archive_path": "old/iTunes Library.xml",
        "sha256": first["itunes_xml"]["sha256"],
        "library_persistent_id": "LIB1",
        "track_count": 1,
        "playlist_count": 1,
    }
    assert first["supported_media_count"] == 1
    assert first["supported_media"][0]["localization_path"] == "iTunes Imported/old/iTunes Media/Artist/Song.mp3"
    assert first["source_kind"] == "zip"
    assert first["package"]["selected_xml_path"] == "old/iTunes Library.xml"
    assert first["package"]["media_files_total"] == 1
    assert first["localization"] == {
        "state": "preview_only",
        "proposed_root": "iTunes Imported",
        "files_total": 1,
        "bytes_total": 5,
        "conflicts": 0,
    }


@pytest.mark.parametrize("name", ["../escape.mp3", "/root.mp3", "C:/drive.mp3", "safe/../../escape.mp3"])
def test_zip_slip_paths_are_rejected(tmp_path, name):
    path = make_zip(tmp_path, [("iTunes Library.xml", XML), (name, b"x")])
    with pytest.raises(ITunesZipImportError, match="absolute|unsafe"):
        inspect_itunes_zip(path)


def test_symlink_is_rejected(tmp_path):
    link = zipfile.ZipInfo("music/link.mp3")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = make_zip(tmp_path, [("iTunes Library.xml", XML), ("ignored", link)])
    with pytest.raises(ITunesZipImportError, match="link or special"):
        inspect_itunes_zip(path)


def test_encrypted_flag_is_rejected_before_member_read(tmp_path):
    path = make_zip(tmp_path, [("iTunes Library.xml", XML)])
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    flags = int.from_bytes(data[central + 8 : central + 10], "little") | 1
    data[central + 8 : central + 10] = flags.to_bytes(2, "little")
    path.write_bytes(data)
    with pytest.raises(ITunesZipImportError, match="encrypted"):
        inspect_itunes_zip(path)


def test_duplicate_and_case_collision_are_rejected(tmp_path):
    duplicate = make_zip(tmp_path, [("iTunes Library.xml", XML), ("a.mp3", b"1"), ("a.mp3", b"2")])
    with pytest.raises(ITunesZipImportError, match="duplicate"):
        inspect_itunes_zip(duplicate)
    collision = make_zip(tmp_path, [("iTunes Library.xml", XML), ("A.mp3", b"1"), ("a.mp3", b"2")])
    with pytest.raises(ITunesZipImportError, match="case-colliding"):
        inspect_itunes_zip(collision)


def test_bomb_entry_and_aggregate_limits_are_rejected(tmp_path):
    path = make_zip(tmp_path, [("iTunes Library.xml", XML), ("zeros.mp3", b"0" * 100_000)])
    with pytest.raises(ITunesZipImportError, match="compression-ratio"):
        inspect_itunes_zip(path, max_compression_ratio=2)
    with pytest.raises(ITunesZipImportError, match="uncompressed-byte"):
        inspect_itunes_zip(path, max_uncompressed_bytes=1_000)
    with pytest.raises(ITunesZipImportError, match="entry limit"):
        inspect_itunes_zip(path, max_entries=1)


def test_ambiguous_xml_requires_explicit_selection(tmp_path):
    path = make_zip(tmp_path, [("one/iTunes Library.xml", XML), ("two/iTunes Library.xml", XML)])
    with pytest.raises(ITunesZipImportError, match="ambiguous"):
        inspect_itunes_zip(path)
    result = inspect_itunes_zip(path, xml_member_path="two/iTunes Library.xml")
    assert result["itunes_xml"]["archive_path"] == "two/iTunes Library.xml"
    assert result["package"]["xml_candidates_total"] == 2


def test_invalid_or_missing_selected_xml_is_rejected(tmp_path):
    path = make_zip(tmp_path, [("bad.xml", b"not xml"), ("song.flac", b"audio")])
    with pytest.raises(ITunesZipImportError, match="does not contain an inspectable"):
        inspect_itunes_zip(path)
    with pytest.raises(ITunesZipImportError, match="not found"):
        inspect_itunes_zip(path, xml_member_path="missing.xml")


def test_manifest_digest_does_not_depend_on_zip_metadata(tmp_path):
    first_path = make_zip(tmp_path, [("iTunes Library.xml", XML), ("song.mp3", b"audio")])
    first = inspect_itunes_zip(first_path)
    first_path.rename(tmp_path / "first.zip")
    second_path = make_zip(tmp_path, [("iTunes Library.xml", XML), ("song.mp3", b"audio")])
    second = inspect_itunes_zip(second_path)
    assert first["manifest_sha256"] == second["manifest_sha256"]
