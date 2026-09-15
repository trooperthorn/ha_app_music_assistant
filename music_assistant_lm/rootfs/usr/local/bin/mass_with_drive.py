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

- ``backup``: copy the drive to ``/share/music-drive-backup/<label>`` with
  rsync and write a manifest (size and SHA-256 of every file).
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


def walk_files(root: Path) -> list[Path]:
    """Every regular file under root, relative, sorted; hidden trash skipped."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file():
                files.append(path.relative_to(root))
    return sorted(files)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(root: Path, manifest: Path) -> int:
    """size, sha256 and path of every file under root; returns the count."""
    files = walk_files(root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="\n") as out:
        for index, rel in enumerate(files, 1):
            path = root / rel
            out.write(f"{path.stat().st_size}\t{sha256_of(path)}\t{rel.as_posix()}\n")
            if index % 500 == 0:
                log(f"manifest: {index}/{len(files)} files hashed")
    return len(files)


def read_manifest(manifest: Path) -> dict[str, tuple[int, str]]:
    entries: dict[str, tuple[int, str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        size, digest, rel = line.split("\t", 2)
        entries[rel] = (int(size), digest)
    return entries


def task_backup(mount_point: Path, backup: Path) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    log(f"backup: copying {mount_point} to {backup} (rsync, resumable)")
    result = subprocess.run(  # noqa: S603
        [
            "rsync",
            "-rt",
            "--modify-window=2",
            "--no-perms",
            "--no-owner",
            "--no-group",
            *(f"--exclude=/{name}" for name in SKIP_DIRS),
            f"{mount_point}/",
            f"{backup}/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(f"backup: rsync exit code {result.returncode}: {result.stderr.strip()[-400:]}")
        log("backup: manifest not written; fix the cause and run the task again")
        return
    count = write_manifest(mount_point, backup / MANIFEST_NAME)
    log(f"backup: done, {count} files copied and hashed; manifest at {backup / MANIFEST_NAME}")
    log("backup: set music_drive_task back to none")


def compare(root: Path, manifest: Path) -> tuple[list[str], list[str], list[str]]:
    """Missing, changed and new files under root against the manifest."""
    expected = read_manifest(manifest)
    present = {rel.as_posix() for rel in walk_files(root)}
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
    if not manifest.is_file():
        log("verify: no manifest; run the backup task first")
        return
    missing, changed, new = compare(mount_point, manifest)
    report = backup / REPORT_NAME
    with report.open("w", encoding="utf-8", newline="\n") as out:
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
    restored = failed = 0
    for line in report.read_text(encoding="utf-8").splitlines():
        kind, _, rel = line.partition("\t")
        if kind not in ("missing", "changed") or not rel:
            continue
        source = backup / rel
        target = mount_point / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            restored += 1
        except OSError as err:
            failed += 1
            log(f"restore: {rel}: {err}")
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
        elif not (backup / MANIFEST_NAME).is_file():
            log("repair: refused, no backup manifest; run the backup task first")
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
            log(f"the {task} task was still running at stop; run it again to finish")
        unmount(mount_point)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
