"""The entrypoint wrapper that mounts the music drive (no devices, no network)."""

from __future__ import annotations

import importlib.util
import json
import shutil
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


def _mark_complete(backup: Path) -> None:
    manifest = backup / wrapper.MANIFEST_NAME
    (backup / wrapper.COMPLETION_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "manifest": wrapper.MANIFEST_NAME,
                "manifest_sha256": wrapper.sha256_of(manifest),
            }
        ),
        encoding="utf-8",
    )


def test_manifest_verify_and_restore_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"A/one.flac": b"one", "A/two.flac": b"two", "B/three.mp3": b"three"})
    # the trash folder is never part of the manifest
    _fill(drive, {".music-assistant-trash/A/old.flac": b"old"})
    _fill(backup, {"A/one.flac": b"one", "A/two.flac": b"two", "B/three.mp3": b"three"})

    count = wrapper.write_manifest(drive, backup / wrapper.MANIFEST_NAME)
    _mark_complete(backup)
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
    assert "verify: no valid completed backup set" in out
    assert "restore: no verify report" in out


def _copying_rsync(source: Path):
    def run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        destination = Path(cmd[-1].rstrip("/"))
        for child in list(destination.iterdir()):
            if child.name in {wrapper.MANIFEST_NAME, wrapper.COMPLETION_NAME, wrapper.REPORT_NAME}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in source.iterdir():
            if child.name in wrapper.SKIP_DIRS:
                continue
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_backup_publishes_only_after_destination_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"A/one.flac": b"one", "B/two.mp3": b"two"})
    backup.mkdir()
    monkeypatch.setattr(wrapper.subprocess, "run", _copying_rsync(drive))

    assert wrapper.task_backup(drive, backup) is True
    assert wrapper.completed_backup_is_valid(backup, verify_files=True)
    metadata = json.loads((backup / wrapper.COMPLETION_NAME).read_text(encoding="utf-8"))
    assert metadata["file_count"] == 2
    assert metadata["total_bytes"] == 6

    (backup / "A/one.flac").write_bytes(b"corrupt")
    assert not wrapper.completed_backup_is_valid(backup, verify_files=True)


def test_backup_rejects_a_source_changed_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"one.flac": b"known-good"})
    backup.mkdir()
    base_run = _copying_rsync(drive)
    monkeypatch.setattr(wrapper.subprocess, "run", base_run)
    assert wrapper.task_backup(drive, backup) is True
    old_manifest = (backup / wrapper.MANIFEST_NAME).read_bytes()
    (drive / "one.flac").write_bytes(b"before")

    def copy_then_change(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        result = base_run(cmd, **kwargs)
        (drive / "one.flac").write_bytes(b"after")
        return result

    monkeypatch.setattr(wrapper.subprocess, "run", copy_then_change)
    assert wrapper.task_backup(drive, backup) is False
    assert wrapper.completed_backup_is_valid(backup, verify_files=True)
    assert (backup / "one.flac").read_bytes() == b"known-good"
    assert (backup / wrapper.MANIFEST_NAME).read_bytes() == old_manifest
    assert "source changed while it was being copied" in capsys.readouterr().out


def test_backup_rejects_destination_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"one.flac": b"source"})
    backup.mkdir()
    base_run = _copying_rsync(drive)

    def corrupt_copy(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        result = base_run(cmd, **kwargs)
        (Path(cmd[-1].rstrip("/")) / "one.flac").write_bytes(b"wrong")
        return result

    monkeypatch.setattr(wrapper.subprocess, "run", corrupt_copy)
    assert wrapper.task_backup(drive, backup) is False
    assert not (backup / wrapper.COMPLETION_NAME).exists()
    assert "destination bytes do not match" in capsys.readouterr().out


def test_interrupted_promotion_recovers_previous_set(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    previous = tmp_path / "backup.previous"
    _fill(previous, {"one.flac": b"known-good"})
    wrapper.write_manifest(previous, previous / wrapper.MANIFEST_NAME, {wrapper.MANIFEST_NAME})
    _mark_complete(previous)

    assert wrapper.completed_backup_is_valid(backup, verify_files=True)
    assert (backup / "one.flac").read_bytes() == b"known-good"
    assert not previous.exists()


def test_restore_rejects_stale_and_unsafe_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"one.flac": b"damaged"})
    _fill(backup, {"one.flac": b"good"})
    wrapper.write_manifest(backup, backup / wrapper.MANIFEST_NAME, {wrapper.MANIFEST_NAME})
    _mark_complete(backup)

    (backup / wrapper.REPORT_NAME).write_text("manifest_sha256\tstale\nchanged\tone.flac\n", encoding="utf-8")
    wrapper.task_restore(drive, backup)
    assert (drive / "one.flac").read_bytes() == b"damaged"
    assert "does not belong to this backup set" in capsys.readouterr().out

    digest = wrapper.sha256_of(backup / wrapper.MANIFEST_NAME)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"leave-me")
    (backup / wrapper.REPORT_NAME).write_text(
        f"manifest_sha256\t{digest}\nchanged\t../outside.flac\n",
        encoding="utf-8",
    )
    wrapper.task_restore(drive, backup)
    assert outside.read_bytes() == b"leave-me"
    assert "refused invalid path" in capsys.readouterr().out


def test_restore_keeps_original_target_when_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    backup = tmp_path / "backup"
    _fill(drive, {"one.flac": b"damaged"})
    _fill(backup, {"one.flac": b"good"})
    wrapper.write_manifest(backup, backup / wrapper.MANIFEST_NAME, {wrapper.MANIFEST_NAME})
    _mark_complete(backup)
    digest = wrapper.sha256_of(backup / wrapper.MANIFEST_NAME)
    (backup / wrapper.REPORT_NAME).write_text(
        f"manifest_sha256\t{digest}\nchanged\tone.flac\n",
        encoding="utf-8",
    )

    def partial_copy(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"partial")
        raise OSError("simulated interruption")

    monkeypatch.setattr(wrapper.shutil, "copyfile", partial_copy)
    wrapper.task_restore(drive, backup)
    assert (drive / "one.flac").read_bytes() == b"damaged"
    assert not list(drive.glob(".*.restore.*.tmp"))


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
    assert "repair: refused, no completed verified backup set" in out
    assert "mounting read-only" in out
    mount_call = next(cmd for cmd in fake_host.calls if cmd[0] == "mount")
    assert mount_call[4].endswith(",ro")
    assert not any(cmd[:2] == ("fsck.exfat", "-y") for cmd in fake_host.calls)

    # with a manifest in place the repair runs before the mount
    manifest = tmp_path / "share" / "musicnix" / wrapper.MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text("", encoding="utf-8")
    _mark_complete(manifest.parent)
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
