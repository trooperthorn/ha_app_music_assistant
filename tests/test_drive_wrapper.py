"""The entrypoint wrapper that mounts the music drive (no devices, no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "music_assistant_lm" / "rootfs" / "usr" / "local" / "bin" / "mass_with_drive.py"

spec = importlib.util.spec_from_file_location("mass_with_drive", WRAPPER)
assert spec and spec.loader
wrapper = importlib.util.module_from_spec(spec)
sys.modules["mass_with_drive"] = wrapper
spec.loader.exec_module(wrapper)


def test_a_label_becomes_a_plain_folder_name() -> None:
    assert wrapper.safe_name("musicnix") == "musicnix"
    assert wrapper.safe_name("My Music (2024)") == "My_Music__2024_"
    assert wrapper.safe_name("") == "drive"
    assert wrapper.safe_name("..") == "drive"


def _fill(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_manifest_verify_and_restore_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"A/one.flac": b"one", "A/two.flac": b"two", "B/three.mp3": b"three"})
    # the trash folder is never part of the manifest
    _fill(drive, {".music-assistant-trash/A/old.flac": b"old"})
    _fill(backup, {"A/one.flac": b"one", "A/two.flac": b"two", "B/three.mp3": b"three"})

    count = wrapper.write_manifest(drive, backup / wrapper.MANIFEST_NAME)
    assert count == 3
    entries = wrapper.read_manifest(backup / wrapper.MANIFEST_NAME)
    assert set(entries) == {"A/one.flac", "A/two.flac", "B/three.mp3"}
    assert entries["A/one.flac"][0] == 3

    # a repair truncates one file and loses another; a new file appears
    (drive / "A/two.flac").write_bytes(b"")
    (drive / "B/three.mp3").unlink()
    _fill(drive, {"C/new.ogg": b"new"})
    wrapper.task_verify(drive, backup)
    report = (backup / wrapper.REPORT_NAME).read_text(encoding="utf-8")
    assert "missing 1, changed 1, new 1" in report
    assert "missing\tB/three.mp3" in report
    assert "changed\tA/two.flac" in report
    assert "new\tC/new.ogg" in report

    wrapper.task_restore(drive, backup)
    assert (drive / "A/two.flac").read_bytes() == b"two"
    assert (drive / "B/three.mp3").read_bytes() == b"three"
    out = capsys.readouterr().out
    assert "restore: 2 files copied back, 0 failed" in out


def test_verify_and_restore_need_their_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wrapper.task_verify(tmp_path / "drive", tmp_path / "backup")
    wrapper.task_restore(tmp_path / "drive", tmp_path / "backup")
    out = capsys.readouterr().out
    assert "verify: no manifest" in out
    assert "restore: no verify report" in out


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Stand-ins for the host: no real mount, blkid or server."""
    calls: list[tuple[str, ...]] = []

    def fake_run(*cmd: str, check: bool = False) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[0] == "blkid":
            value = {"TYPE": "exfat", "LABEL": "musicnix"}[cmd[2]]
            return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")
        if cmd[0] == "fsck.exfat":
            return SimpleNamespace(returncode=state.fsck_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    state = SimpleNamespace(calls=calls, fsck_rc=0, served=None, execd=None)
    monkeypatch.setattr(wrapper, "run", fake_run)
    monkeypatch.setattr(wrapper, "wait_for_device", lambda device, timeout_s=0: True)
    monkeypatch.setattr(wrapper, "MOUNT_ROOT", tmp_path / "music")
    monkeypatch.setattr(wrapper, "BACKUP_ROOT", tmp_path / "share")
    monkeypatch.setattr(wrapper, "run_server", lambda argv: state.__setattr__("served", argv) or 0)
    monkeypatch.setattr(wrapper.os, "execv", lambda path, argv: state.__setattr__("execd", (path, argv)))
    return state


def test_without_a_drive_the_original_entrypoint_runs(fake_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wrapper, "read_options", lambda: {"music_drive": None})
    wrapper.main(["--data-dir", "/data"])
    assert fake_host.execd == (wrapper.ENTRYPOINT, [wrapper.ENTRYPOINT, "--data-dir", "/data"])
    assert fake_host.calls == []


def test_a_clean_drive_mounts_read_write_under_its_label(
    fake_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wrapper, "read_options", lambda: {"music_drive": "/dev/sda2", "music_drive_task": "none"})
    assert wrapper.main(["--data-dir", "/data"]) == 0
    mount_call = next(cmd for cmd in fake_host.calls if cmd[0] == "mount")
    assert mount_call == (
        "mount",
        "-t",
        "exfat",
        "-o",
        "nosuid,nodev,noexec,relatime,umask=000,iocharset=utf8",
        "/dev/sda2",
        str(tmp_path / "music" / "musicnix"),
    )
    assert fake_host.served == ["--data-dir", "/data"]
    # unmounted after the server exits
    assert ("umount", str(tmp_path / "music" / "musicnix")) in fake_host.calls
    assert "exfat check on /dev/sda2: clean" in capsys.readouterr().out


def test_a_dirty_drive_mounts_read_only_and_repair_needs_a_backup(
    fake_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_host.fsck_rc = 1
    monkeypatch.setattr(wrapper, "read_options", lambda: {"music_drive": "/dev/sda2", "music_drive_task": "repair"})
    wrapper.main([])
    out = capsys.readouterr().out
    assert "repair: refused, no backup manifest" in out
    assert "mounting read-only" in out
    mount_call = next(cmd for cmd in fake_host.calls if cmd[0] == "mount")
    assert mount_call[4].endswith(",ro")
    assert not any(cmd[:2] == ("fsck.exfat", "-y") for cmd in fake_host.calls)

    # with a manifest in place the repair runs before the mount
    manifest = tmp_path / "share" / "musicnix" / wrapper.MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text("", encoding="utf-8")
    fake_host.calls.clear()
    wrapper.main([])
    order = [cmd[:2] for cmd in fake_host.calls if cmd[0] in ("fsck.exfat", "mount")]
    assert order[0] == ("fsck.exfat", "-n")
    assert ("fsck.exfat", "-y") in order
    assert order.index(("fsck.exfat", "-y")) < order.index(("mount", "-t"))


def test_an_unknown_task_or_filesystem_is_refused(
    fake_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wrapper, "read_options", lambda: {"music_drive": "/dev/sda2", "music_drive_task": "format"})
    wrapper.main([])
    assert "unknown music_drive_task 'format'" in capsys.readouterr().out

    def other_probe(device: str) -> tuple[str, str]:
        return "hfsplus", "mac"

    monkeypatch.setattr(wrapper, "probe", other_probe)
    monkeypatch.setattr(wrapper, "read_options", lambda: {"music_drive": "/dev/sda2"})
    fake_host.calls.clear()
    wrapper.main([])
    assert "starting without it" in capsys.readouterr().out
    assert not any(cmd[0] == "mount" for cmd in fake_host.calls)
