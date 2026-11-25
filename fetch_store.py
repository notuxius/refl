"""Local releases, stable output files, and recoverable publication."""

import ctypes
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from fetch_sources import digest, filename, stage_release, windows_product_version
from shelf_cancel import JobCanceled, cancel_scope, check_canceled, read_chunks
from shelf_message import error_detail, message
from shelf_shortcuts import shortcut_path, write_shortcut


ISO_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]*")
WAITING_FOR_CLOSE = "Waiting for the app to close"
DOWNGRADE_SKIPPED = "Downgrade skipped"
SHARING_ERRORS = {32, 33}


def is_versioned_iso_target(path):
    return path is not None and path.suffix.lower() == ".iso" and path.stem.endswith(" {version}")


def target_for_version(path, version):
    if not is_versioned_iso_target(path):
        return path
    if not ISO_VERSION.fullmatch(version):
        raise ValueError(message("Invalid filename: {value!r}", value=version))
    name = path.stem.removesuffix("{version}") + version + path.suffix
    return path.with_name(filename(name))


def read_state(root, name):
    path = root / name
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(state, dict) or not isinstance(state.get("version"), str)
            or not isinstance(state.get("sha256"), dict)
            or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("release", "")))):
        raise ValueError(message("Invalid release manifest: {path}", path=path))
    return state


def write_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".manifest-", delete=False) as handle:
        temp = Path(handle.name)
        try:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def fetch_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".fetch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(message("Could not lock {root}; another fetch may be running", root=root)) from exc
        yield


def stamp(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def file_issue(path, checksum, recorded, full):
    if not path.is_file():
        return message("Missing file: {path}", path=path)
    if not checksum:
        return message("No SHA-256 recorded for {path}", path=path)
    if not full and recorded == stamp(path):
        return ""
    if digest(path) != checksum:
        return message("SHA-256 mismatch: {path}", path=path)
    return ""


def cache_issue(root, recipe, state, archive, full):
    folder = root / "releases" / state["release"]
    if not folder.is_dir():
        return message("Missing release folder: {folder}", folder=folder)
    names = recipe.versioned_members(state["version"]).expected_files(archive)
    if recipe.extract_folder:
        names = set(names).union(tree_members(state))
    for name in names:
        issue = file_issue(folder / name, state["sha256"].get(name), state.get("stats", {}).get(name), full)
        if issue:
            return issue
    return ""


def tree_members(state):
    for name in state["sha256"]:
        if name.startswith("files/"):
            for part in name[6:].split("/"):
                filename(part)
            yield name


def check_link(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"Unexpected link in item directory: {path}")


def retry_readonly_removal(remove, path, error):
    if (getattr(error, "winerror", None) != 5 or remove not in (os.unlink, os.rmdir)
            or not os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY):
        raise error
    os.chmod(path, stat.S_IWRITE, follow_symlinks=False)
    try:
        remove(path)
    except OSError:
        os.chmod(path, stat.S_IREAD, follow_symlinks=False)
        raise


def prune_legacy_archive(root, folder, recipe, output):
    """Older builds could leave a second published copy of a versioned archive."""
    if recipe.extract or output.path.parent != root / "current":
        return
    contents = list(folder.iterdir())
    if len(contents) != 1:
        return
    archive = contents[0]
    old = output.path.parent / archive.name
    pattern = recipe.archive.replace("{version}", "*") or recipe.asset_pattern
    if old == output.path or Path(old.name) in output.companions or not fnmatchcase(old.name, pattern):
        return
    check_link(old.parent)
    check_link(old)
    check_link(archive)
    if (old.is_file() and archive.is_file() and old.stat().st_size == archive.stat().st_size
            and digest(old) == digest(archive)):
        old.unlink()


def prune_releases(root, recipe, output, current, pending=None):
    """Called under the fetch lock. Keep both sides of an unfinished publication."""
    check_link(root)
    releases = root / "releases"
    check_link(releases)
    keep = {entry["release"] for entry in (current, pending) if entry}
    for folder in releases.iterdir():
        if folder.name in keep or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        check_link(folder)
        if folder.is_dir():
            if not pending:
                prune_legacy_archive(root, folder, recipe, output)
            shutil.rmtree(folder, onexc=retry_readonly_removal)
    previous = root / ".previous-app"
    if current and not pending and previous.exists():
        check_link(previous)
        shutil.rmtree(previous, onexc=retry_readonly_removal)


def remove_superseded_output(root, previous, output):
    published = (previous or {}).get("published", {})
    path = Path(published.get("path", ""))
    if path == output.path or not published:
        return
    versioned_iso = output.template and path == target_for_version(output.template, previous["version"])
    if not versioned_iso and path.parent not in (root / "current", root / "bin"):
        return
    if path.parent == output.path.parent and (output.tree or Path(path.name) in output.companions):
        return
    # Only retire the managed primary if it still contains the bytes we published.
    check_link(root)
    check_link(path.parent)
    check_link(path)
    hashes = previous["sha256"]
    checksum = (hashes.get(previous.get("archive")) if versioned_iso else
                hashes.get("files/" + path.name) or hashes.get(path.name))
    if checksum and path.is_file() and digest(path) == checksum:
        path.unlink()


def companion_files(source, target):
    files = {}
    if source is None:
        return files
    check_link(source.parent)
    check_link(source)
    if not source.exists():
        return files
    if target.parent.resolve().is_relative_to(source.resolve()):
        raise ValueError("The app folder must be outside its companion folder.")

    def collect(directory):
        for path in directory.iterdir():
            check_canceled()
            check_link(path)
            relative = path.relative_to(source)
            live = target.parent / relative
            check_link(live)
            if relative.parts[0].casefold() == target.name.casefold():
                raise ValueError(f"A companion file conflicts with the published item: {relative}")
            if path.is_dir():
                collect(path)
            elif path.is_file():
                files[relative] = path
            else:
                raise ValueError(f"Unsupported companion file: {path}")
    check_link(target.parent)
    collect(source)
    return files


def portable_settings(recipe, files):
    settings = {p: source for p, source in files.items() if recipe.is_portable_setting(p)}
    if recipe.apply_custom_settings:
        for relative in recipe.settings_files or ():
            if relative not in files:
                raise ValueError(f"Missing portable config: {relative}")
        if not settings:
            raise ValueError("No portable configuration files found.")
    return settings


def apply_published_settings(recipe, dest):
    """Apply the user's saved choice offline, or defer until the item is published."""
    root = recipe.cache_root(dest)
    if not recipe.apply_custom_settings or not (root / "current.json").exists():
        return False
    with fetch_lock(root):
        state = read_state(root, "current.json") or {}
        path = state.get("published", {}).get("path")
        if not path or not recipe.matches_fingerprint(state.get("recipe")) or not Path(path).is_file():
            return False
        files = portable_settings(recipe, companion_files(recipe.companion_dir, Path(path)))
        publish_companions(files, Path(path).parent, files, True)
    return True


def publish_companions(files, directory, settings, apply_settings):
    """Seed support files once and replace only selected portable configurations."""
    for relative, source in files.items():
        target = directory / relative
        setting = relative in settings
        if setting and not apply_settings:
            continue
        if target.is_file() and (not setting or digest(target) == digest(source)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".companion-", delete=False) as handle:
            temp = Path(handle.name)
        try:
            shutil.copy2(source, temp)
            if setting:
                temp.replace(target)
            elif os.name == "nt":
                temp.rename(target)  # Refuses to overwrite a concurrently created file.
            else:
                os.link(temp, target)
        except FileExistsError:
            if setting or not target.is_file():
                raise
        finally:
            temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class Output:
    member: str
    path: Path
    tree: bool = False
    shortcut: Path | None = None
    companions: dict = field(default_factory=dict)
    settings: frozenset = frozenset()
    apply_settings: bool = False
    template: Path | None = None

    @classmethod
    def for_release(cls, root, recipe, archive, override, state, version):
        member = recipe.primary(archive)
        default = root / "current" / Path(member).name
        published = (state or {}).get("published", {})
        if recipe.publish_exe and Path(published.get("path", "")) != default:
            default = root / "bin" / recipe.publish_exe
        if recipe.extract_folder and override:
            raise ValueError("A file target cannot publish a complete folder. Remove this item's file mapping.")
        template = override if is_versioned_iso_target(override) else None
        if template and (recipe.extract or Path(archive).suffix.lower() != ".iso"):
            raise ValueError("A versioned ISO target requires an ISO download without extraction.")
        target = target_for_version(override, version) or default
        if template:
            check_link(target)
        tree = bool(recipe.extract_folder or (len(recipe.extract) > 1 and override is None))
        files = companion_files(recipe.companion_dir, target)
        settings = frozenset(portable_settings(recipe, files))
        return cls(member, target, tree,
                   shortcut_path(recipe.shortcut) if recipe.shortcut else None,
                   files, settings, recipe.apply_custom_settings, template)

    def companion_issue(self, state):
        for relative, source in self.companions.items():
            target = self.path.parent / relative
            if relative in self.settings:
                if self.apply_settings:
                    if not target.is_file() or digest(target) != digest(source):
                        return message("Apply custom settings: {path}", path=target)
                    continue
                if not self.tree or "files/" + relative.as_posix() not in state["sha256"]:
                    continue
            if not target.is_file():
                return message("Missing file: {path}", path=target)
        return ""

    def issue(self, state, full):
        published = state.get("published", {})
        if self.path != Path(published.get("path", "")):
            return message("Publish the verified file to {path}", path=self.path)
        issue = file_issue(self.path, state["sha256"].get(self.member), published.get("stat"), full)
        if not issue and self.tree:
            for name in tree_members(state):
                if Path(name[6:]) in self.companions:
                    continue
                issue = file_issue(self.path.parent / name[6:], state["sha256"][name],
                                   published.get("stats", {}).get(name), full)
                if issue:
                    break
        if not issue:
            if issue := self.companion_issue(state):
                return issue
            if self.tree and sorted(p.as_posix() for p in self.companions) != published.get("companions", []):
                return "Publish companion files"
        if not issue and self.shortcut:
            link = published.get("shortcut", {})
            if self.shortcut != Path(link.get("path", "")):
                return f"Create Start menu shortcut: {self.shortcut}"
            issue = file_issue(self.shortcut, link.get("sha256"), link.get("stat"), full)
        return issue


def matches(state, recipe, release):
    return (state is not None and state["version"] == release.version
            and state.get("modified", "") == release.modified
            and recipe.matches_fingerprint(state.get("recipe"))
            and state.get("archive", recipe.archive) == release.archive
            and (not release.checksum or state["sha256"].get(release.archive) == release.checksum))


def version_order(version):
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", version):
        return "date", version
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} UTC", version):
        return "timestamp", version
    match = re.fullmatch(r"[vV]?([0-9]{1,16}(?:\.[0-9]{1,16}){0,7})"
                         r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?", version)
    if match is None:
        return None
    number, prerelease = match.groups()
    parts = tuple(map(int, number.split(".")))
    parts += (0,) * (8 - len(parts))
    identifiers = tuple((0, len(part.lstrip("0")), part.lstrip("0")) if part.isdecimal() else (1, part)
                        for part in prerelease.split(".")) if prerelease else ()
    return "number", (parts, prerelease is None, identifiers)


def version_is_newer(installed, offered):
    left, right = version_order(installed), version_order(offered)
    return left is not None and right is not None and left[0] == right[0] and left[1] > right[1]


def saved_version_is_newer(recipe, state, offered):
    return (state is not None and recipe.matches_fingerprint(state.get("recipe"))
            and version_is_newer(state["version"], offered))


def installed_versions(target, candidate):
    if os.name != "nt" or target.suffix.lower() not in {".exe", ".dll", ".scr", ".cpl", ".ocx"} or not target.is_file():
        return None
    try:
        installed = windows_product_version(target, fixed_fallback=True)
        offered = windows_product_version(candidate, fixed_fallback=True)
    except (OSError, ValueError):
        return None
    if version_is_newer(installed, offered):
        return installed, offered


class DowngradeSkipped(Exception):
    pass


def protect_installed_version(target, candidate):
    if versions := installed_versions(target, candidate):
        raise DowngradeSkipped(*versions)


def downgrade_result(installed, offered, available):
    return Result(DOWNGRADE_SKIPPED, installed, available,
                  message("Kept version {installed} because {offered} is older.",
                          installed=installed, offered=offered))


def discard_downgrade(root, candidate, current):
    pending = read_state(root, "pending.json")
    if pending and pending["release"] == candidate["release"]:
        (root / "pending.json").unlink()
    if not current or current["release"] != candidate["release"]:
        folder = root / "releases" / candidate["release"]
        check_link(root)
        check_link(folder.parent)
        check_link(folder)
        if folder.is_dir():
            shutil.rmtree(folder, onexc=retry_readonly_removal)


def download(root, recipe, release, on_progress=None):
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    release_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(dir=root, prefix=".stage-") as temp:
        folder = Path(temp) / "payload"
        folder.mkdir()
        members = recipe.versioned_members(release.version)
        archive_checksum = stage_release(members, release, folder, on_progress)
        names = members.expected_files(release.archive)
        if recipe.extract_folder:
            names = [release.archive, *(p.relative_to(folder).as_posix() for p in
                                        (folder / "files").rglob("*") if p.is_file())]
        state = {"version": release.version, "release": release_id, "recipe": recipe.fingerprint(),
                 "archive": release.archive, "sha256": {
                     name: archive_checksum if name == release.archive else digest(folder / name) for name in names},
                 "stats": {name: stamp(folder / name) for name in names}}
        if release.modified:
            state["modified"] = release.modified
        check_canceled()
        folder.rename(releases / release_id)
    return state


class PublishPending(Exception):
    pass


@cache
def windows_file_handles():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    open_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    return open_file, kernel.CloseHandle


def file_in_use(path):
    if os.name != "nt":
        return False
    open_file, close_handle = windows_file_handles()
    # A write request detects mapped executables. DELETE detects handles that block renames.
    handle = open_file(str(path), 0x40010000, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() in SHARING_ERRORS
    close_handle(handle)
    return False


def publication_lock(error, output):
    code = getattr(error, "winerror", None)
    if code != 5 and code not in SHARING_ERRORS:
        return None
    paths = [Path(path) for path in (error.filename2, error.filename) if path]
    for path in paths:
        if file_in_use(path):
            return path
        if output.tree and path == output.path.parent:
            if locked := next((child for child in path.rglob("*") if file_in_use(child)), None):
                return locked
    if code in SHARING_ERRORS:
        return paths[0] if paths else output.path
    return None


def publish_tree(root, folder, state, output):
    """Switch the staged app into place, preserving files the user or app added."""
    live = output.path.parent
    previous = root / ".previous-app"
    if any(path.is_symlink() or path.is_junction() for path in (live, previous)):
        raise ValueError("Unexpected link at the item directory")
    if previous.exists() and not live.exists():
        previous.rename(live)
    names = list(tree_members(state))
    old_state = read_state(root, "current.json") or {"sha256": {}}
    old_published = old_state.get("published", {})
    old_files = {Path(name[6:]) for name in tree_members(old_state) if name in old_published.get("stats", {})}
    if old_published.get("path") and Path(old_published["path"]).parent == live:
        old_files.add(Path(Path(old_published["path"]).name))
    managed = [name for name in names if Path(name[6:]) not in output.companions]
    obsolete = old_files.difference((Path(name[6:]) for name in names), output.companions)
    previous_companions = old_published.get("companions", [])
    companions = sorted(p.as_posix() for p in output.companions)
    if (not any((live / path).exists() for path in obsolete)
            and companions == previous_companions
            and not output.companion_issue(state)
            and all(not file_issue(live / name[6:], state["sha256"][name], None, True) for name in managed)):
        return {name: stamp(live / name[6:]) for name in managed}
    # Windows can rename a directory containing a running executable but cannot then clean it up.
    for path in old_files:
        if path.suffix.lower() in {".exe", ".dll"} and file_in_use(live / path):
            raise PermissionError(13, "File is in use", str(live / path), 32)
    with tempfile.TemporaryDirectory(dir=root, prefix=".publish-") as staging:
        ready = Path(staging) / "app"
        shutil.copytree(folder / "files", ready)
        for name in names:
            if digest(ready / name[6:]) != state["sha256"][name]:
                raise ValueError(f"Invalid staged item file: {name}")
        for relative, source in output.companions.items():
            existing = live / relative
            if existing.is_dir():
                raise ValueError(f"Companion file conflicts with an existing directory: {relative}")
            key = relative.as_posix()
            # On first import replace a pristine publisher default with the saved settings.
            old_default = old_state["sha256"].get("files/" + key)
            if relative in output.settings:
                if not output.apply_settings:
                    if not existing.is_file():
                        continue  # Keep the package default, if the publisher supplies one.
                    source = existing
            elif existing.is_file() and (key in previous_companions or not old_default or digest(existing) != old_default):
                source = existing
            destination = ready / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_dir():
                raise ValueError(f"Companion file conflicts with a package directory: {relative}")
            shutil.copy2(source, destination)
        if live.exists():
            for path in live.rglob("*"):
                relative = path.relative_to(live)
                if path.is_symlink() or path.is_junction():
                    raise ValueError(f"Unexpected link in item directory: {path}")
                if relative in old_files or (ready / relative).exists():
                    continue
                if path.is_dir():
                    (ready / relative).mkdir(parents=True, exist_ok=True)
                else:
                    (ready / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, ready / relative)
            protect_installed_version(output.path, folder / output.member)
            if previous.exists():
                shutil.rmtree(previous)
            live.rename(previous)
        try:
            ready.rename(live)
        except OSError:
            if previous.exists():
                previous.rename(live)
            raise
    return {name: stamp(live / name[6:]) for name in managed}


def publish(root, recipe, state, output, archive, previous):
    folder = root / "releases" / state["release"]
    state["archive"] = archive
    state.pop("publication_lock", None)
    if "stats" not in state:
        state["stats"] = {name: stamp(folder / name) for name in recipe.versioned_members(state["version"]).expected_files(archive)}
    target = output.path
    source = folder / output.member
    checksum = state["sha256"][output.member]
    temp = None
    try:
        if not output.tree:
            target.parent.mkdir(parents=True, exist_ok=True)
        if not output.tree and file_issue(target, checksum, None, True):
            previous_path = (previous or {}).get("published", {}).get("path")
            if output.template and target.exists() and (not previous_path or target != Path(previous_path)):
                raise ValueError(f"A different file already exists at {target}")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".releaseshelf-", delete=False) as handle:
                temp = Path(handle.name)
                with source.open("rb") as stream:
                    for chunk in read_chunks(stream):
                        handle.write(chunk)
            if digest(temp) != checksum:
                raise ValueError(message("SHA-256 mismatch while publishing {target}", target=target))
        # Once journaled, finish the commit even if the UI requests cancellation.
        check_canceled()
        if output.tree or temp is not None:
            protect_installed_version(target, source)
        write_state(root / "pending.json", state)
        with cancel_scope(None):
            if output.tree:
                stats = publish_tree(root, folder, state, output)
            else:
                publish_companions(output.companions, target.parent, output.settings, output.apply_settings)
                if temp is not None:
                    protect_installed_version(target, source)
                    os.replace(temp, target)
            state["published"] = {"path": str(target), "stat": stamp(target)}
            if output.tree:
                state["published"]["stats"] = stats
                if output.companions:
                    state["published"]["companions"] = sorted(p.as_posix() for p in output.companions)
            if output.shortcut:
                write_shortcut(output.shortcut, target)
                state["published"]["shortcut"] = {"path": str(output.shortcut), "stat": stamp(output.shortcut),
                                                  "sha256": digest(output.shortcut)}
            remove_superseded_output(root, previous, output)
    except ValueError:
        # Keep a verified release available for retry if the publication copy is damaged.
        write_state(root / "pending.json", state)
        raise
    except OSError as exc:
        if locked := publication_lock(exc, output):
            state["publication_lock"] = {"target": str(target), "path": str(locked)}
        write_state(root / "pending.json", state)
        if locked:
            raise PublishPending(message("Publication pending: {path}", path=target)) from exc
        raise PublishPending(message("Verified download saved. Close any program using "
                                     "{target} or check write access, then run fetch again. {error}", target=target, error=str(exc))) from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    write_state(root / "current.json", state)
    (root / "pending.json").unlink(missing_ok=True)


@dataclass
class Result:
    status: str
    local: str = "-"
    available: str = "-"
    detail: str = ""
    locked_path: Path | None = None

    @property
    def display_status(self):
        return WAITING_FOR_CLOSE if self.locked_path is not None else self.status


def pending_result(pending, local, detail):
    lock = pending.get("publication_lock")
    if isinstance(lock, dict) and isinstance(lock.get("path"), str) and isinstance(lock.get("target"), str):
        path = Path(lock["path"])
        if path.is_absolute() and "\x00" not in lock["path"]:
            return Result("Pending", local, pending["version"],
                          message("Publication pending: {path}", path=lock["target"]), path)
    return Result("Pending", local, pending["version"], detail)


def run_release(action, root, recipe, release, override, on_progress=None, *, defer_download=False):
    """Return the resolved release if a network transfer is required and deferred."""
    state = read_state(root, "current.json")
    pending = read_state(root, "pending.json")
    local = state["version"] if state else "-"
    if saved_version_is_newer(recipe, state, release.version):
        if action == "fetch" and matches(pending, recipe, release):
            discard_downgrade(root, pending, state)
        return downgrade_result(local, release.version, release.version)
    output = Output.for_release(root, recipe.versioned_members(release.version), release.archive, override,
                                state or pending, release.version)
    same = matches(state, recipe, release)
    cached_issue = cache_issue(root, recipe, state, release.archive, action == "fetch") if same else None
    issue = cached_issue or ""
    if same and not issue:
        issue = output.issue(state, action == "fetch")
        if not issue and pending is None:
            if action == "fetch":
                prune_releases(root, recipe, output, state)
            return Result("Latest", local, release.version, str(output.path))

    if not state:
        reason = message("Missing manifest: {path}", path=root / "current.json")
    elif local != release.version:
        reason = message("Saved version {local}, available version {available}", local=local, available=release.version)
    else:
        reason = issue or message("Recipe or release asset changed")
    if action == "check":
        if issue and same and not cached_issue:
            source = root / "releases" / state["release"] / output.member
            if versions := installed_versions(output.path, source):
                return downgrade_result(*versions, release.version)
        if matches(pending, recipe, release):
            return pending_result(pending, local, message("Publication pending: {path}", path=output.path))
        return Result("Repair needed" if same else "Update available", local, release.version, reason)

    candidate = None
    if matches(pending, recipe, release):
        pending_issue = (cached_issue if same and pending["release"] == state["release"]
                         and pending["sha256"] == state["sha256"]
                         else cache_issue(root, recipe, pending, release.archive, True))
        if not pending_issue:
            candidate = pending.copy()
    if candidate is None and same and not cached_issue:
        candidate = state.copy()
    if candidate is None:
        if defer_download:
            return release
        candidate = download(root, recipe, release, on_progress)
        status = "Updated"
    else:
        status = "Cached"
    detail = message("Fetch needed: {reason}\n{path}", reason=reason, path=output.path)
    return publish_release(root, recipe, candidate, output, release.archive, state, status, detail)


def publish_release(root, recipe, candidate, output, archive, state, status, detail):
    try:
        publish(root, recipe, candidate, output, archive, state)
    except JobCanceled:
        if status == "Updated":
            shutil.rmtree(root / "releases" / candidate["release"])
        raise
    except DowngradeSkipped as exc:
        discard_downgrade(root, candidate, state)
        return downgrade_result(*exc.args, candidate["version"])
    except PublishPending as exc:
        prune_releases(root, recipe, output, state, candidate)
        return pending_result(candidate, state["version"] if state else "-", error_detail(exc))
    with cancel_scope(None):
        prune_releases(root, recipe, output, candidate)
    return Result(status, candidate["version"], candidate["version"], detail)


def resume_publication(root, recipe, override):
    with fetch_lock(root):
        state = read_state(root, "current.json")
        pending = read_state(root, "pending.json")
        local = state["version"] if state else "-"
        if not pending:
            return Result("Latest" if state else "Unknown", local)
        result = pending_result(pending, local, "Run Get updates to retry publication.")
        if result.locked_path is None or file_in_use(result.locked_path):
            return result
        if not recipe.matches_fingerprint(pending.get("recipe")):
            return Result("Repair needed", local, detail="Recipe differs from the saved release")
        archive = filename(pending.get("archive", recipe.archive))
        version = pending["version"]
        if saved_version_is_newer(recipe, state, version):
            discard_downgrade(root, pending, state)
            return downgrade_result(local, version, version)
        output = Output.for_release(root, recipe.versioned_members(version), archive, override, state or pending, version)
        if output.path != Path(pending["publication_lock"]["target"]):
            return Result("Repair needed", local,
                          detail=message("Publish the verified file to {path}", path=output.path))
        if issue := cache_issue(root, recipe, pending, archive, True):
            return Result("Repair needed", local, version, issue)
        return publish_release(root, recipe, pending, output, archive, state, "Cached", str(output.path))


def verify(root, recipe, override):
    state = read_state(root, "current.json")
    pending = read_state(root, "pending.json")
    if not state:
        detail = message("Missing manifest: {path}", path=root / "current.json")
        return pending_result(pending, "-", detail) if pending else Result("Repair needed", detail=detail)
    local = state["version"]
    if not recipe.matches_fingerprint(state.get("recipe")):
        return Result("Repair needed", local, detail="Recipe differs from the saved release")
    archive = filename(state.get("archive", recipe.archive))
    output = Output.for_release(root, recipe.versioned_members(local), archive, override, state, local)
    issue = cache_issue(root, recipe, state, archive, True)
    if not issue:
        issue = output.issue(state, True)
        if issue:
            source = root / "releases" / state["release"] / output.member
            if versions := installed_versions(output.path, source):
                return downgrade_result(*versions, local)
    if issue:
        return Result("Repair needed", local, detail=issue)
    if pending:
        return pending_result(pending, local, "Publication pending. Run fetch to finish.")
    return Result("Verified", local, detail=message("SHA-256 verified: {path}", path=output.path))
