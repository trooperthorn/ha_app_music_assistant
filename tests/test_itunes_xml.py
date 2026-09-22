"""Legacy iTunes XML inspection boundaries."""

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "music_assistant_lm/providers/library_enrichment/itunes_xml.py"
spec = importlib.util.spec_from_file_location("itunes_xml", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_library(tmp_path, body):
    path = tmp_path / "iTunes Library.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict>' + body + "</dict></plist>", encoding="utf-8"
    )
    return path


def fixture_body():
    return """
    <key>Library Persistent ID</key><string>LIBRARY123</string>
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer><key>Persistent ID</key><string>TRACKA</string>
        <key>Name</key><string>Café Song</string><key>Artist</key><string>Artist</string>
        <key>Location</key><string>file://localhost/F:/%5BArchive%5D/iTunesold/iTunes%20Media/A/Cafe.mp3</string></dict>
      <key>2</key><dict><key>Track ID</key><integer>2</integer><key>Name</key><string>Old Track</string>
        <key>Kind</key><string>MPEG audio file</string><key>Location</key><string>file://localhost/f:/Other/Old.mp3</string></dict>
      <key>3</key><dict><key>Track ID</key><integer>3</integer><key>Persistent ID</key><string>VIDEO</string>
        <key>Kind</key><string>MPEG-4 video file</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Favorites</string><key>Playlist Persistent ID</key><string>PLAYLIST1</string>
        <key>Playlist Items</key><array>
          <dict><key>Track ID</key><integer>1</integer></dict><dict><key>Track ID</key><integer>1</integer></dict>
          <dict><key>Track ID</key><integer>404</integer></dict><dict><key>Track ID</key><integer>3</integer></dict>
        </array></dict>
      <dict><key>Name</key><string>Folder</string><key>Folder</key><true/></dict>
      <dict><key>Name</key><string>Smart</string><key>Smart Info</key><string>x</string>
        <key>Playlist Items</key><array><dict><key>Track ID</key><integer>2</integer></dict></array></dict>
    </array>
    """


def test_preview_preserves_identity_order_duplicates_and_missing(tmp_path):
    result = module.inspect_itunes_xml(
        write_library(tmp_path, fixture_body()),
        path_mappings=[
            {"source_prefix": "F:/[Archive]/", "target_prefix": "old", "provider_instance_id": "fs"},
            {"source_prefix": "F:/[Archive]/iTunesold/iTunes Media/", "target_prefix": "Music", "provider_instance_id": "fs"},
        ],
    )
    assert result["library_id"] == "LIBRARY123"
    assert result["track_count"] == 3 and result["occurrence_count"] == 5
    assert result["path_roots"] == [{"root": "F:/", "source_root": "F:/", "track_count": 2}]
    assert result["roots"] == [{"source_root": "F:/", "track_count": 2}]
    assert result["source_digest"] == result["source"]["sha256"]
    assert result["tracks_total"] == 3 and result["playlists_total"] == 3
    assert result["mapping_summary"] == {"matched": 2, "unresolved": 2, "ambiguous": 0, "unsupported": 1}
    playlist = result["playlists"][0]
    assert playlist["source_playlist_id"] == "PLAYLIST1"
    assert playlist["id"] == "PLAYLIST1" and playlist["track_count"] == 4
    assert playlist["duplicate_occurrence_count"] == 1
    assert [row["position"] for row in playlist["occurrences"]] == [0, 1, 2, 3]
    assert [row["state"] for row in playlist["occurrences"]] == ["track", "track", "missing_reference", "unsupported"]
    mapped = playlist["occurrences"][0]["path"]
    assert mapped["provider_item_id"] == "Music/A/Cafe.mp3"
    assert mapped["source_prefix"] == "F:/[Archive]/iTunesold/iTunes Media/"
    assert result["tracks"]["2"]["source_item_id"] == "track-id:2"
    assert result["tracks"]["2"]["identity_fallback"] is True


def test_playlist_classification_and_deterministic_digest(tmp_path):
    path = write_library(tmp_path, fixture_body())
    first = module.inspect_itunes_xml(path)
    second = module.inspect_itunes_xml(path)
    assert first["source"]["sha256"] == second["source"]["sha256"]
    assert first["playlists"][0]["snapshot_id"] == second["playlists"][0]["snapshot_id"]
    assert [(row["classification"], row["importable"]) for row in first["playlists"]] == [
        ("user", True), ("folder", False), ("smart_snapshot", True)
    ]


def test_standard_apple_plist_doctype_is_accepted_without_resolving_it(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>Tracks</key><dict></dict>'
        '<key>Playlists</key><array></array></dict></plist>',
        encoding="utf-8",
    )
    assert module.inspect_itunes_xml(path)["tracks_total"] == 0


@pytest.mark.parametrize(
    "xml,match",
    [
        ('<!DOCTYPE plist [<!ENTITY x "boom">]><plist><dict><key>A</key><string>&x;</string></dict></plist>', "DTD"),
        ("<plist><dict><key>A</key><string>x</string>", "malformed"),
        ("<plist><dict><key>A</key><string>x</string><key>A</key><string>y</string></dict></plist>", "duplicate key"),
        ("<plist><dict><key>A</key><date>not-a-date</date></dict></plist>", "invalid date"),
    ],
)
def test_unsafe_or_invalid_xml_is_rejected(tmp_path, xml, match):
    path = tmp_path / "bad.xml"
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(module.ITunesXMLImportError, match=match):
        module.inspect_itunes_xml(path)


def test_size_and_mapping_contracts_are_bounded(tmp_path):
    path = write_library(tmp_path, fixture_body())
    with pytest.raises(module.ITunesXMLImportError, match="size limit"):
        module.inspect_itunes_xml(path, max_bytes=path.stat().st_size - 1)
    with pytest.raises(module.ITunesXMLImportError, match="requires"):
        module.inspect_itunes_xml(path, path_mappings=[{"source_prefix": "F:/"}])


def test_playlist_rename_keeps_source_identity_stable(tmp_path):
    def body(name):
        return f"""
        <key>Tracks</key><dict></dict>
        <key>Playlists</key><array>
          <dict><key>Name</key><string>{name}</string>
            <key>Playlist Persistent ID</key><string>STABLEID</string>
            <key>Playlist Items</key><array></array></dict>
        </array>
        """

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    before = module.inspect_itunes_xml(write_library(first_dir, body("Road Trip")))
    after = module.inspect_itunes_xml(write_library(second_dir, body("Summer Road Trip 2026")))
    assert before["playlists"][0]["source_playlist_id"] == after["playlists"][0]["source_playlist_id"] == "STABLEID"
    assert before["playlists"][0]["name"] == "Road Trip"
    assert after["playlists"][0]["name"] == "Summer Road Trip 2026"
    assert before["playlists"][0]["identity_fallback"] is False
    assert after["playlists"][0]["identity_fallback"] is False


def test_repeated_source_track_survives_multiple_occurrences(tmp_path):
    body = """
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer><key>Persistent ID</key><string>TRACKA</string>
        <key>Location</key><string>file://localhost/F:/Music/Song.mp3</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Repeats</string><key>Playlist Persistent ID</key><string>PL</string>
        <key>Playlist Items</key><array>
          <dict><key>Track ID</key><integer>1</integer></dict>
          <dict><key>Track ID</key><integer>1</integer></dict>
          <dict><key>Track ID</key><integer>1</integer></dict>
          <dict><key>Track ID</key><integer>404</integer></dict>
          <dict><key>Track ID</key><integer>404</integer></dict>
        </array></dict>
    </array>
    """
    result = module.inspect_itunes_xml(
        write_library(tmp_path, body),
        path_mappings=[{"source_prefix": "F:/Music/", "target_prefix": "Music", "provider_instance_id": "fs"}],
    )
    playlist = result["playlists"][0]
    assert [row["source_item_id"] for row in playlist["occurrences"][:3]] == ["TRACKA"] * 3
    assert [row["state"] for row in playlist["occurrences"]] == ["track", "track", "track", "missing_reference", "missing_reference"]
    # duplicate_occurrence_count only tallies resolved occurrences (missing references have no source_item_id key in `seen`)
    assert playlist["duplicate_occurrence_count"] == 2
    assert playlist["occurrence_count"] == 5


@pytest.mark.parametrize("kind", ["Podcast", "Audiobook file", "TV Show", "MPEG-4 movie file"])
def test_podcast_book_and_video_kinds_are_all_classified_unsupported(tmp_path, kind):
    body = f"""
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer><key>Kind</key><string>{kind}</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Mixed</string><key>Playlist Persistent ID</key><string>PL</string>
        <key>Playlist Items</key><array><dict><key>Track ID</key><integer>1</integer></dict></array></dict>
    </array>
    """
    result = module.inspect_itunes_xml(write_library(tmp_path, body))
    assert result["tracks"]["1"]["media_type"] == "unsupported"
    assert result["playlists"][0]["occurrences"][0]["state"] == "unsupported"
    assert result["mapping_summary"]["unsupported"] == 1


def test_disjoint_old_roots_map_independently_into_the_same_target(tmp_path):
    body = """
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer>
        <key>Location</key><string>file://localhost/F:/Music/A.mp3</string></dict>
      <key>2</key><dict><key>Track ID</key><integer>2</integer>
        <key>Location</key><string>file://localhost/G:/OldStuff/B.mp3</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Combined</string><key>Playlist Persistent ID</key><string>PL</string>
        <key>Playlist Items</key><array>
          <dict><key>Track ID</key><integer>1</integer></dict>
          <dict><key>Track ID</key><integer>2</integer></dict>
        </array></dict>
    </array>
    """
    result = module.inspect_itunes_xml(
        write_library(tmp_path, body),
        path_mappings=[
            {"source_prefix": "F:/Music/", "target_prefix": "Music", "provider_instance_id": "fs"},
            {"source_prefix": "G:/OldStuff/", "target_prefix": "Music", "provider_instance_id": "fs"},
        ],
    )
    assert {row["source_root"] for row in result["roots"]} == {"F:/", "G:/"}
    occurrences = result["playlists"][0]["occurrences"]
    assert occurrences[0]["path"]["provider_item_id"] == "Music/A.mp3"
    assert occurrences[1]["path"]["provider_item_id"] == "Music/B.mp3"
    assert result["mapping_summary"] == {"matched": 2, "unresolved": 0, "ambiguous": 0, "unsupported": 0}


def test_lowercase_and_uppercase_drive_letters_merge_into_one_root(tmp_path):
    body = """
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer>
        <key>Location</key><string>file://localhost/F:/Music/A.mp3</string></dict>
      <key>2</key><dict><key>Track ID</key><integer>2</integer>
        <key>Location</key><string>file://localhost/f:/Music/B.mp3</string></dict>
    </dict>
    <key>Playlists</key><array></array>
    """
    result = module.inspect_itunes_xml(write_library(tmp_path, body))
    assert result["path_roots"] == [{"root": "F:/", "source_root": "F:/", "track_count": 2}]


def test_mapping_prefix_matches_regardless_of_case_and_accepts_backslash_separators(tmp_path):
    body = """
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer>
        <key>Location</key><string>file://localhost/F:/Music/Song.mp3</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Case</string><key>Playlist Persistent ID</key><string>PL</string>
        <key>Playlist Items</key><array><dict><key>Track ID</key><integer>1</integer></dict></array></dict>
    </array>
    """
    result = module.inspect_itunes_xml(
        write_library(tmp_path, body),
        path_mappings=[{"source_prefix": "f:\\music\\", "target_prefix": "Media\\Sub", "provider_instance_id": "fs"}],
    )
    mapped = result["playlists"][0]["occurrences"][0]["path"]
    assert mapped["state"] == "mapped"
    assert mapped["provider_item_id"] == "Media/Sub/Song.mp3"


def test_percent_encoded_unicode_location_decodes_correctly(tmp_path):
    body = """
    <key>Tracks</key><dict>
      <key>1</key><dict><key>Track ID</key><integer>1</integer>
        <key>Location</key><string>file://localhost/F:/Musique/Chanson%20%C3%A9t%C3%A9.mp3</string></dict>
    </dict>
    <key>Playlists</key><array>
      <dict><key>Name</key><string>Unicode</string><key>Playlist Persistent ID</key><string>PL</string>
        <key>Playlist Items</key><array><dict><key>Track ID</key><integer>1</integer></dict></array></dict>
    </array>
    """
    result = module.inspect_itunes_xml(
        write_library(tmp_path, body),
        path_mappings=[{"source_prefix": "F:/Musique/", "target_prefix": "Musique", "provider_instance_id": "fs"}],
    )
    mapped = result["playlists"][0]["occurrences"][0]["path"]
    assert mapped["provider_item_id"] == "Musique/Chanson été.mp3"


def test_missing_library_identity_and_playlist_identity_have_stable_fallbacks(tmp_path):
    body = """
      <key>Tracks</key><dict></dict><key>Playlists</key><array>
      <dict><key>Name</key><string>Loose</string><key>Playlist ID</key><integer>9</integer>
      <key>Playlist Items</key><array></array></dict></array>
    """
    path = write_library(tmp_path, body)
    result = module.inspect_itunes_xml(path)
    assert result["library_id"] == f"sha256:{result['source']['sha256']}"
    assert result["library_identity_fallback"] is True
    assert result["playlists"][0]["source_playlist_id"] == "playlist-id:9"
    assert result["playlists"][0]["identity_fallback"] is True
