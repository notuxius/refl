"""Find and stage updates to the portable Windows build of Relf."""

from base64 import b64encode
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from urllib.error import HTTPError

from fetch_sources import (GitHubRateLimited, defer_github_checks, digest, open_url, read_page,
                           sha256, windows_product_version, write_chunks)
from shelf_cancel import cancel_scope, publisher_items, read_chunks
from shelf_version import __version__


LATEST_RELEASE = "https://api.github.com/repos/notuxius/refl/releases/latest"
RELEASE_DOWNLOAD = "https://github.com/notuxius/refl/releases/download"
EXE_NAME = "Relf.exe"


@dataclass(frozen=True)
class RelfUpdate:
    version: str
    notes: str
    size: int
    checksum: str


def relf_executable():
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def check_for_update():
    token = defer_github_checks.set(True)
    try:
        release = json.loads(read_page(LATEST_RELEASE, headers={"Cache-Control": "no-cache"}))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except GitHubRateLimited as exc:
        raise ValueError("GitHub is limiting update checks. Try again later.") from exc
    finally:
        defer_github_checks.reset(token)
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = re.fullmatch(r"v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))", release["tag_name"])
    if not tag:
        raise ValueError("The GitHub release has an invalid Relf version.")
    version = tag[1]
    if tuple(map(int, version.split("."))) <= tuple(map(int, __version__.split("."))):
        return None
    assets = [asset for asset in release["assets"]
              if asset["name"] == EXE_NAME and asset["state"] == "uploaded"]
    if len(assets) != 1:
        raise ValueError("The GitHub release must include one Relf.exe.")
    asset, = assets
    size = asset["size"]
    algorithm, _, checksum = (asset.get("digest") or "").partition(":")
    if algorithm != "sha256" or type(size) is not int or size <= 0:
        raise ValueError("The GitHub release is missing verification information for Relf.exe.")
    notes = release.get("body") or ""
    if not isinstance(notes, str):
        raise ValueError("The GitHub release notes could not be read.")
    return RelfUpdate(version, notes, size, sha256(checksum))


def download_update(update, staged, *, on_progress=None):
    url = f"{RELEASE_DOWNLOAD}/v{update.version}/{EXE_NAME}"

    def chunks():
        with open_url(url) as response:
            yield from read_chunks(response)

    with staged.open("xb") as target:
        write_chunks(publisher_items(chunks), target, update.size, on_progress)
    verify_update(update, staged)
    if windows_product_version(staged) != update.version:
        raise ValueError("The downloaded Relf.exe version does not match the GitHub release.")


def verify_update(update, staged):
    if staged.stat().st_size != update.size or digest(staged) != update.checksum:
        raise ValueError("The downloaded Relf.exe did not pass SHA-256 verification.")


def stage_update(executable, update, *, on_progress=None, cancel=None):
    stage = Path(tempfile.mkdtemp(prefix=".relf-update-", dir=executable.parent))
    try:
        with cancel_scope(cancel):
            download_update(update, stage / EXE_NAME, on_progress=on_progress)
    except Exception:
        discard_update(stage)
        raise
    return stage


def discard_update(stage):
    with suppress(OSError):
        (stage / EXE_NAME).unlink(missing_ok=True)
    with suppress(OSError):
        stage.rmdir()


def install_update(executable, update, stage):
    try:
        verify_update(update, stage / EXE_NAME)
    except (OSError, ValueError):
        discard_update(stage)
        raise
    try:
        # PyInstaller one-file builds run both a launcher and a GUI process.
        return start_update_helper(executable, stage, (os.getpid(), os.getppid()), sys.argv[1:])
    except Exception:
        for name in ("armed", "error"):
            with suppress(OSError):
                (stage / name).unlink(missing_ok=True)
        raise


def start_update_helper(executable, stage, pids, args):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = Path(__file__).with_name("replace_relf.ps1").read_text(encoding="utf-8")
    command = b64encode(script.encode("utf-16le")).decode("ascii")
    path_key = hashlib.sha256(str(executable).casefold().encode("utf-8")).hexdigest()
    env = {**os.environ, "RELF_UPDATER_EXE": str(executable), "RELF_UPDATER_STAGE": str(stage),
           "RELF_UPDATER_PIDS": ",".join(map(str, pids)), "RELF_UPDATER_LOCK": f"Local\\Relf.Update.{path_key}",
           "RELF_UPDATER_ARGS": subprocess.list2cmdline(args)}
    process = subprocess.Popen(
        [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        env=env, creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = monotonic() + 10
        while process.poll() is None and monotonic() < deadline:
            if (stage / "armed").is_file():
                return process
            sleep(.05)
        detail = stage / "error"
        raise OSError(detail.read_text(encoding="utf-8") if detail.is_file()
                      else "The Windows update helper could not start.")
    except Exception:
        process.kill()
        process.wait()
        raise


def finish_relaunch():
    """Tell the helper the new window opened, or return a rollback error."""
    if ready := os.environ.pop("RELF_UPDATE_READY", None):
        Path(ready).touch(exist_ok=False)
    return os.environ.pop("RELF_UPDATE_FAILURE", "")
