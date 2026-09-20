#!/app/venv/bin/python
"""
Mount the music drive, run the server, unmount on exit.

The app's ``music_drive`` option names a block device (a partition picked in
the app's configuration page). This wrapper replaces the server image's
entrypoint: it mounts that partition under ``/music/<label>`` before the
server starts, runs the original entrypoint as a child, forwards the stop
signal, and unmounts once the server has exited. Without a drive configured
it hands over to the original entrypoint unchanged.

Nothing here writes to the drive on its own. A dirty exFAT volume (not
cleanly ejected) is mounted read-only and the log says so. Writing only
happens through the ``music_drive_task`` option, one task per start, each
logged and each leaving a report under the backup folder:

- ``backup``: copy the drive into a staging set, independently hash source and
  destination, then publish it at ``/share/music-drive-backup/<label>`` only
  when the source stayed stable and every copied byte matches.
- ``verify``: hash the drive again and report what is missing, changed or
  new against the manifest.
- ``restore``: copy every file the last verify reported missing or changed
  back from the backup.
- ``repair``: run the exFAT check with repair before mounting; refused unless
  a manifest exists, so a backup always comes first.

Set the task back to ``none`` afterwards; the log says so as well.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")
ENTRYPOINT = "/usr/local/bin/entrypoint.sh"
MOUNT_ROOT = Path("/music")
BACKUP_ROOT = Path("/share/music-drive-backup")
MANIFEST_NAME = "manifest.sha256"
COMPLETION_NAME = "backup-set.json"
REPORT_NAME = "verify-report.txt"
TASKS = ("none", "backup", "verify", "restore", "repair")
# the drive is internal, but enumeration can trail the container start
DEVICE_WAIT_S = 30
# mount options for every filesystem; the drive holds music, nothing to run
BASE_OPTIONS = "nosuid,nodev,noexec,relatime"
# filesystems without ownership: open to the server, UTF-8 names
NO_OWNER_OPTIONS = "umask=000,iocharset=utf8"
# the trash folder the fork's duplicates page moves files into; never backed up
SKIP_DIRS = {".music-assistant-trash"}


def log(message: str) -> None:
    print(f"[music-drive] {message}", flush=True)


def run(*cmd: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its output for the log."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)  # noqa: S603


def read_options(path: Path = OPTIONS_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for_device(device: str, timeout_s: float = DEVICE_WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while not Path(device).exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


def probe(device: str) -> tuple[str, str]:
    """The partition's filesystem type and label, from its superblock."""
    fstype = run("blkid", "-s", "TYPE", "-o", "value", device).stdout.strip()
    label = run("blkid", "-s", "LABEL", "-o", "value", device).stdout.strip()
    return fstype, label or "drive"


def safe_name(label: str) -> str:
    """A label as a folder name: letters, digits, dash, underscore, dot."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in label).strip(".")
    return cleaned or "drive"


def check_exfat(device: str) -> bool:
    """Read-only check; True when the volume is clean. Writes nothing."""
    result = run("fsck.exfat", "-n", device)
    clean = result.returncode == 0
    detail = (result.stdout + result.stderr).strip().splitlines()
    log(f"exfat check on {device}: {'clean' if clean else 'not clean'}" + (f" ({detail[-1]})" if detail else ""))
    return clean


def repair_exfat(device: str) -> bool:
    """The exFAT check with repair; only called for the explicit repair task."""
    result = run("fsck.exfat", "-y", device)
    tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
    for line in tail:
        log(f"repair: {line}")
    log(f"repair finished with exit code {result.returncode}")
    return result.returncode == 0


def mount(device: str, fstype: str, target: Path, read_only: bool) -> bool:
    options = BASE_OPTIONS
    if fstype in ("exfat", "vfat", "msdos", "ntfs"):
        options += "," + NO_OWNER_OPTIONS
    if read_only:
        options += ",ro"
    target.mkdir(parents=True, exist_ok=True)
    mount_type = "ntfs3" if fstype == "ntfs" else fstype
    result = run("mount", "-t", mount_type, "-o", options, device, str(target))
    if result.returncode != 0:
        log(f"mount of {device} at {target} failed: {result.stderr.strip() or result.stdout.strip()}")
        return False
    log(f"{device} ({fstype}) mounted at {target}{' read-only' if read_only else ''}")
    return True


def unmount(target: Path) -> None:
    result = run("umount", str(target))
    if result.returncode != 0:
        log(f"unmount of {target} did not complete cleanly, detaching: {result.stderr.strip()}")
        run("umount", "-l", str(target))
    else:
        log(f"{target} unmounted")


# ---- tasks -------------------------------------------------------------------


def walk_files(root: Path, exclude_root_files: set[str] | None = None) -> list[Path]:
    """Every regular file under root, relative, sorted; hidden trash skipped."""
    exclude_root_files = exclude_root_files or set()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            if path.is_file() and not (len(rel.parts) == 1 and name in exclude_root_files):
                files.append(rel)
    return sorted(files)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(root: Path, manifest: Path, exclude_root_files: set[str] | None = None) -> int:
    """size, sha256 and path of every file under root; returns the count."""
    files = walk_files(root, exclude_root_files)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="\n") as out:
        for index, rel in enumerate(files, 1):
            path = root / rel
            out.write(f"{path.stat().st_size}\t{sha256_of(path)}\t{rel.as_posix()}\n")
            if index % 500 == 0:
                log(f"manifest: {index}/{len(files)} files hashed")
    return len(files)


def manifests_match(left: Path, right: Path) -> bool:
    """Return whether two complete snapshots describe identical bytes."""
    return read_manifest(left) == read_manifest(right)


def _temporary_manifest(backup: Path, purpose: str) -> Path:
    """A same-filesystem temporary path suitable for an atomic publish."""
    return backup.parent / f".{backup.name}.{purpose}.{os.getpid()}.tmp"


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_tree_if_present(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def read_manifest(manifest: Path) -> dict[str, tuple[int, str]]:
    entries: dict[str, tuple[int, str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        size, digest, rel = line.split("\t", 2)
        entries[rel] = (int(size), digest)
    return entries


def _published_marker_is_valid(backup: Path) -> bool:
    manifest = backup / MANIFEST_NAME
    completion = backup / COMPLETION_NAME
    try:
        metadata = json.loads(completion.read_text(encoding="utf-8"))
        return bool(
            metadata.get("schema_version") == 1
            and metadata.get("status") == "complete"
            and metadata.get("manifest") == MANIFEST_NAME
            and metadata.get("manifest_sha256") == sha256_of(manifest)
        )
    except (OSError, ValueError, TypeError):
        return False


def recover_interrupted_promotion(backup: Path) -> None:
    """Restore the prior verified set if power was lost during promotion."""
    previous = backup.parent / f"{backup.name}.previous"
    if not backup.exists() and _published_marker_is_valid(previous):
        os.replace(previous, backup)
        log(f"backup: recovered the previous verified set at {backup}")


def completed_backup_is_valid(backup: Path, verify_files: bool = False) -> bool:
    """Validate the published marker and, optionally, every destination byte."""
    recover_interrupted_promotion(backup)
    manifest = backup / MANIFEST_NAME
    try:
        if not _published_marker_is_valid(backup):
            return False
        if not verify_files:
            return True
        missing, changed, new = compare(
            backup,
            manifest,
            {MANIFEST_NAME, COMPLETION_NAME, REPORT_NAME},
        )
        return not (missing or changed or new)
    except (OSError, ValueError, TypeError):
        return False


def task_backup(mount_point: Path, backup: Path) -> bool:
    """Copy and independently validate a stable source snapshot."""
    backup.parent.mkdir(parents=True, exist_ok=True)
    recover_interrupted_promotion(backup)
    candidate = backup.parent / f".{backup.name}.candidate.{os.getpid()}"
    previous = backup.parent / f"{backup.name}.previous"
    _remove_tree_if_present(candidate)
    candidate.mkdir()
    manifest = candidate / MANIFEST_NAME
    completion = candidate / COMPLETION_NAME
    source_before = _temporary_manifest(backup, "source-before")
    source_after = _temporary_manifest(backup, "source-after")
    destination = _temporary_manifest(backup, "destination")
    completion_tmp = _temporary_manifest(backup, "completion")
    control_files = {MANIFEST_NAME, COMPLETION_NAME, REPORT_NAME}
    try:
        log("backup: hashing the source before copy")
        count = write_manifest(mount_point, source_before)
        log(f"backup: copying {mount_point} to a private staging set (rsync, resumable)")
        result = subprocess.run(  # noqa: S603
            [
                "rsync",
                "-rt",
                "--delete",
                "--modify-window=2",
                "--no-perms",
                "--no-owner",
                "--no-group",
                *(f"--exclude=/{name}" for name in SKIP_DIRS),
                f"{mount_point}/",
                f"{candidate}/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log(f"backup: rsync exit code {result.returncode}: {result.stderr.strip()[-400:]}")
            log("backup: no completed backup set was published; fix the cause and run the task again")
            return False

        log("backup: independently hashing the copied destination")
        write_manifest(candidate, destination, control_files)
        log("backup: confirming the source did not change during the copy")
        write_manifest(mount_point, source_after)
        if not manifests_match(source_before, source_after):
            log("backup: source changed while it was being copied; no completed backup set was published")
            return False
        if not manifests_match(source_before, destination):
            log("backup: destination bytes do not match the source; no completed backup set was published")
            return False

        os.replace(destination, manifest)
        manifest_digest = sha256_of(manifest)
        total_bytes = sum(size for size, _digest in read_manifest(manifest).values())
        completion_tmp.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "complete",
                    "file_count": count,
                    "total_bytes": total_bytes,
                    "manifest": MANIFEST_NAME,
                    "manifest_sha256": manifest_digest,
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(completion_tmp, completion)
        _remove_tree_if_present(previous)
        if backup.exists():
            os.replace(backup, previous)
        try:
            os.replace(candidate, backup)
        except OSError:
            if previous.exists() and not backup.exists():
                os.replace(previous, backup)
            raise
        log(f"backup: complete and verified, {count} files; recovery set at {backup}")
        log("backup: set music_drive_task back to none")
        return True
    finally:
        for temporary in (source_before, source_after, destination, completion_tmp):
            _remove_if_present(temporary)
        _remove_tree_if_present(candidate)


def compare(
    root: Path,
    manifest: Path,
    exclude_root_files: set[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Missing, changed and new files under root against the manifest."""
    expected = read_manifest(manifest)
    present = {rel.as_posix() for rel in walk_files(root, exclude_root_files)}
    missing = sorted(set(expected) - present)
    new = sorted(present - set(expected))
    changed: list[str] = []
    for index, rel in enumerate(sorted(set(expected) & present), 1):
        path = root / rel
        size, digest = expected[rel]
        if path.stat().st_size != size or sha256_of(path) != digest:
            changed.append(rel)
        if index % 500 == 0:
            log(f"verify: {index} files checked")
    return missing, changed, new


def task_verify(mount_point: Path, backup: Path) -> None:
    manifest = backup / MANIFEST_NAME
    if not completed_backup_is_valid(backup, verify_files=True):
        log("verify: no valid completed backup set; run the backup task first")
        return
    missing, changed, new = compare(mount_point, manifest)
    report = backup / REPORT_NAME
    with report.open("w", encoding="utf-8", newline="\n") as out:
        out.write(f"manifest_sha256\t{sha256_of(manifest)}\n")
        out.write(f"verify of {mount_point} against {manifest} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        out.write(f"missing {len(missing)}, changed {len(changed)}, new {len(new)}\n")
        for label, items in (("missing", missing), ("changed", changed), ("new", new)):
            for rel in items:
                out.write(f"{label}\t{rel}\n")
    log(f"verify: missing {len(missing)}, changed {len(changed)}, new {len(new)}; report at {report}")
    log("verify: set music_drive_task back to none" + (", then restore to copy the affected files back" if missing or changed else ""))


def task_restore(mount_point: Path, backup: Path) -> None:
    report = backup / REPORT_NAME
    if not report.is_file():
        log("restore: no verify report; run the verify task first")
        return
    if not completed_backup_is_valid(backup, verify_files=True):
        log("restore: backup destination verification failed; run the backup task again")
        return
    lines = report.read_text(encoding="utf-8").splitlines()
    expected_report_header = f"manifest_sha256\t{sha256_of(backup / MANIFEST_NAME)}"
    if not lines or lines[0] != expected_report_header:
        log("restore: verify report does not belong to this backup set; run verify again")
        return
    manifest_entries = read_manifest(backup / MANIFEST_NAME)
    restored = failed = 0
    for line in lines[1:]:
        kind, _, rel = line.partition("\t")
        if kind not in ("missing", "changed") or not rel:
            continue
        rel_path = Path(rel)
        if rel not in manifest_entries or rel_path.is_absolute() or ".." in rel_path.parts:
            failed += 1
            log(f"restore: refused invalid path {rel!r}")
            continue
        source = backup / rel
        target = mount_point / rel
        temporary = target.parent / f".{target.name}.restore.{os.getpid()}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, temporary)
            size, digest = manifest_entries[rel]
            if temporary.stat().st_size != size or sha256_of(temporary) != digest:
                raise OSError("copied bytes do not match the backup manifest")
            os.replace(temporary, target)
            restored += 1
        except OSError as err:
            failed += 1
            log(f"restore: {rel}: {err}")
        finally:
            _remove_if_present(temporary)
    log(f"restore: {restored} files copied back, {failed} failed; run verify again to confirm")
    log("restore: set music_drive_task back to none")


def run_task(task: str, mount_point: Path, backup: Path, read_only: bool) -> None:
    if task == "backup":
        task_backup(mount_point, backup)
    elif task == "verify":
        task_verify(mount_point, backup)
    elif task == "restore":
        if read_only:
            log("restore: the drive is mounted read-only (not clean); repair first")
            return
        task_restore(mount_point, backup)


# ---- main --------------------------------------------------------------------


def run_server(argv: list[str]) -> int:
    """The original entrypoint as a child; the stop signal reaches it."""
    child = subprocess.Popen([ENTRYPOINT, *argv])  # noqa: S603

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return child.wait()


def main(argv: list[str]) -> int:
    options = read_options()
    device = options.get("music_drive") or ""
    task = options.get("music_drive_task") or "none"
    if task not in TASKS:
        log(f"unknown music_drive_task {task!r}; nothing run")
        task = "none"

    if not device:
        os.execv(ENTRYPOINT, [ENTRYPOINT, *argv])  # noqa: S606
        return 0  # only reached when execv is stubbed (tests)

    if not wait_for_device(device):
        log(f"{device} did not appear within {DEVICE_WAIT_S} s; starting without the drive")
        return run_server(argv)

    fstype, label = probe(device)
    if fstype not in ("exfat", "vfat", "msdos", "ntfs", "ext4", "ext3", "ext2", "btrfs", "xfs"):
        log(f"{device} has filesystem {fstype or 'unknown'}, which this app does not mount; starting without it")
        return run_server(argv)

    mount_point = MOUNT_ROOT / safe_name(label)
    backup = BACKUP_ROOT / safe_name(label)

    clean = check_exfat(device) if fstype == "exfat" else True
    if task == "repair":
        if fstype != "exfat":
            log("repair: only exFAT is repaired here")
        elif not completed_backup_is_valid(backup, verify_files=True):
            log("repair: refused, no completed verified backup set; run the backup task first")
        elif clean:
            log("repair: the volume is already clean; nothing to do")
        else:
            clean = repair_exfat(device)
            log("repair: set music_drive_task back to none, then run verify")
        task = "none"

    read_only = fstype == "exfat" and not clean
    if read_only:
        log("the volume is not clean; mounting read-only. A backup, then the repair task, makes it writable")
    if not mount(device, fstype, mount_point, read_only):
        return run_server(argv)
    log(f"point the Filesystem provider at a folder under {mount_point}")

    worker: threading.Thread | None = None
    if task != "none":
        worker = threading.Thread(target=run_task, args=(task, mount_point, backup, read_only), daemon=True)
        worker.start()

    try:
        return run_server(argv)
    finally:
        if worker and worker.is_alive():
            log(f"waiting for the {task} task to finish before unmounting")
            worker.join()
        unmount(mount_point)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
