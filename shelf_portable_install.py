"""Install portable Windows packages into the current user's Programs folder."""

from contextlib import suppress
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from fetch_sources import CONFIG_SUFFIXES, Recipe, digest, extract_windows_archive, filename
from fetch_store import check_link, fetch_lock, read_state, retry_readonly_removal, write_state
from shelf_message import message
from shelf_shortcuts import shortcut_path, write_shortcut


PORTABLE_ARCHIVES = {".zip", ".7z", ".rar"}
MANIFEST = ".relf-portable.json"


def portable_folder(name, item_id):
    safe_name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "-", name).rstrip(" .")
    try:
        safe_name = filename(safe_name)
    except ValueError:
        safe_name = filename(item_id)
    programs = Path(os.environ["LOCALAPPDATA"]) / "Programs"
    if not programs.is_absolute():
        raise ValueError("Could not locate the current user's Programs folder.")
    return programs / safe_name


def check_path(path):
    for part in (*reversed(path.parents), path):
        check_link(part)


def read_install(folder, ids):
    if not folder.exists():
        return None
    marker = folder / MANIFEST
    check_path(marker)
    if marker.is_file():
        data = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id") in ids:
            return data
    raise ValueError(message("A different app folder already exists at {path}.", path=folder))


def installed_launcher(name, ids):
    folder = portable_folder(name, ids[0])
    if state := read_install(folder, ids):
        relative = Path(state["launcher"])
        if not relative.is_absolute() and not relative.anchor and ".." not in relative.parts:
            launcher = folder / relative
            check_path(launcher)
            if launcher.is_file():
                return launcher
    return None


def checked_copy(source, destination):
    def check_entries(folder, names):
        for name in names:
            check_link(Path(folder) / name)
        return ()

    shutil.copytree(source, destination, ignore=check_entries)


def keep_portable_data(folder, ready, members):
    for directory, subfolders, files in folder.walk():
        for name in (*subfolders, *files):
            check_link(directory / name)
        for name in files:
            original = directory / name
            relative = original.relative_to(folder)
            if relative == Path(MANIFEST):
                continue
            bundled = relative.as_posix().casefold() in members
            if bundled and original.suffix.lower() not in CONFIG_SUFFIXES:
                continue
            target = ready / relative
            if target.is_dir() or (target.exists() and not bundled):
                raise ValueError(message("A new package file conflicts with {path}.", path=original))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)


def install_portable_item(job, source, dest, choose):
    name = job.name or job.id
    if not isinstance(job, Recipe):
        return install_portable(source, name, job.id_keys(), choose)
    cache = job.cache_root(dest)
    with fetch_lock(cache):
        state = read_state(cache, "current.json") or {}
        published = state.get("published", {})
        checksum = None
        if Path(published.get("path", "")) == source:
            member = published.get("member") or state.get("archive") or source.name
            checksum = state["sha256"].get(member)
        if not checksum:
            raise ValueError(message("No SHA-256 recorded for {path}", path=source))
        return install_portable(source, name, job.id_keys(), choose, checksum=checksum)


def install_portable(source, name, ids, choose, *, checksum=None):
    folder = portable_folder(name, ids[0])
    check_path(folder)
    check_path(source)
    if source.is_relative_to(folder) or folder.is_relative_to(source):
        raise ValueError(message("Choose an install folder separate from {path}.", path=source))
    link = shortcut_path(folder.name)
    check_path(link)
    locks = folder.parent.parent / "Relf" / "portable-installs"
    check_path(locks / ".fetch.lock")
    backup = folder.with_name(".relf-previous-" + sha256(folder.name.casefold().encode()).hexdigest()[:24])
    check_path(backup)
    folder.parent.mkdir(parents=True, exist_ok=True)
    with fetch_lock(locks), tempfile.TemporaryDirectory(prefix=".relf-install-", dir=folder.parent) as stage:
        previous = read_install(backup, ids)
        if previous and not folder.exists():
            backup.rename(folder)
            previous = None
        saved = read_install(folder, ids)
        if not saved and link.exists():
            raise ValueError(message("A different Start menu shortcut already exists at {path}.", path=link))
        if checksum is not None and digest(source) != checksum:
            raise ValueError(message("SHA-256 mismatch: {path}", path=source))
        ready = Path(stage) / "app"
        if source.is_dir():
            checked_copy(source, ready)
        else:
            extract_windows_archive(source, ready)
        if (ready / MANIFEST).exists():
            raise ValueError(message("The package uses the reserved filename {name}.", name=MANIFEST))
        files = sorted((path.relative_to(ready) for path in ready.rglob("*") if path.is_file()),
                       key=lambda path: (len(path.parts), path.as_posix().casefold()))
        launchers = tuple(path.as_posix() for path in files if path.suffix.lower() == ".exe"
                          and not path.stem.casefold().startswith(("unins", "uninstall"))
                          and not path.stem.casefold().endswith(("setup", "installer"))
                          and path.stem.casefold() != "install")
        if not launchers:
            raise ValueError(message("No portable Windows executable was found in {name}.", name=source.name))
        remembered = (saved or {}).get("launcher")
        if remembered in launchers:
            launcher = remembered
        else:
            launcher = launchers[0] if len(launchers) == 1 else choose(launchers)
        if launcher is None:
            return None
        if launcher not in launchers:
            raise ValueError("Choose an executable from the package.")
        if saved:
            keep_portable_data(folder, ready, set(saved["members"]))
        write_state(ready / MANIFEST, {"id": ids[0], "launcher": launcher,
                                      "members": [path.as_posix().casefold() for path in files]})
        if previous:
            shutil.rmtree(backup, onexc=retry_readonly_removal)
        if saved:
            folder.rename(backup)
        try:
            ready.rename(folder)
            try:
                write_shortcut(link, folder / launcher, owned_folder=folder)
            except Exception:
                folder.rename(ready)
                raise
        except Exception:
            if saved:
                backup.rename(folder)
            raise
        if saved:
            with suppress(OSError):
                shutil.rmtree(backup, onexc=retry_readonly_removal)
        return folder / launcher
