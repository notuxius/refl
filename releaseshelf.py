"""Relf: check publishers, fetch portable apps, or verify local files."""

import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, replace
from http.client import HTTPException
from itertools import islice
from pathlib import Path
from threading import Lock
from time import sleep

from fetch_sources import GitHubRateLimited, Recipe, Release, defer_github_checks, filename, github_wait_seconds
from fetch_store import (DOWNGRADE_SKIPPED, ISO_VERSION, Result, check_link, fetch_lock, is_folder_target,
                         is_versioned_iso_target, matches, previous_app_folder, read_state, resume_publication,
                         run_release, verify)
from shelf_cancel import JobCanceled, cancel_scope, check_canceled, publisher_backpressure, publisher_items
from shelf_checklist import Checklist, LocalItem, catalog_item
from shelf_message import error_detail, message


ERRORS = (OSError, ValueError, LookupError, TypeError, AttributeError, re.error,
          zipfile.BadZipFile, HTTPException, RuntimeError)
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
DEFAULT_CATALOG = APP_DIR / "recipes"
START_PARALLEL_ITEMS = 3
MAX_PARALLEL_ITEMS = 6
UPDATE_RETRY_DELAYS = (1, 2, 4)
GITHUB_PAUSED = "Waiting for GitHub rate limit reset…"
DEFAULT_PREVIOUS_VERSIONS = 1


def batch_map(action, worker, items, *, on_github_pause=None, is_canceled=None):
    if action == "verify":
        yield from map(worker, items)
        return
    if action == "resume":
        with ThreadPoolExecutor(max_workers=START_PARALLEL_ITEMS) as pool:
            yield from pool.map(worker, items)
        return

    limit, clean, backoffs = START_PARALLEL_ITEMS, 0, 0
    lock = Lock()

    def back_off():
        nonlocal limit, clean, backoffs
        with lock:
            limit = max(1, limit // 2)
            clean = 0
            backoffs += 1

    def run(item, previous_backoffs):
        nonlocal limit, clean
        token = publisher_backpressure.set(back_off)
        github_token = defer_github_checks.set(True)
        try:
            result = worker(item)
        finally:
            defer_github_checks.reset(github_token)
            publisher_backpressure.reset(token)
        if result.status in {"Latest", "Update available", "Repair needed", "Updated", "Cached", "Pending",
                             DOWNGRADE_SKIPPED}:
            with lock:
                # Measure recovery with jobs admitted after the last slowdown.
                if previous_backoffs == backoffs:
                    clean += 1
                    if clean == 3:
                        limit = min(MAX_PARALLEL_ITEMS, limit + 1)
                        clean = 0
        return result

    remaining = enumerate(items)
    active, ready, github_paused = {}, {}, {}
    next_result = 0
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_ITEMS) as pool:
        while True:
            delay = github_wait_seconds() if github_paused else 0
            with lock:
                for index, item in tuple(github_paused.items()):
                    if len(active) >= limit:
                        break
                    if not delay or (is_canceled is not None and is_canceled(item)):
                        del github_paused[index]
                        active[pool.submit(run, item, backoffs)] = index, item
                for index, item in islice(remaining, max(0, limit - len(active))):
                    active[pool.submit(run, item, backoffs)] = index, item
            while next_result in ready:
                yield ready.pop(next_result).result()
                next_result += 1
            if not active and not github_paused:
                return
            wake_after = None
            if github_paused and len(active) < limit:
                wake_after = github_wait_seconds()
                if is_canceled is not None:
                    wake_after = min(wake_after, 0.1)
            if not active:
                sleep(wake_after)
                continue
            done, _ = wait(active, timeout=wake_after, return_when=FIRST_COMPLETED)
            for future in done:
                index, item = active.pop(future)
                if isinstance(future.exception(), GitHubRateLimited):
                    if on_github_pause is not None:
                        on_github_pause(item)
                    github_paused[index] = item
                else:
                    ready[index] = future


def updates_disabled(job):
    return isinstance(job, Recipe) and job.updates_disabled


def destination_preference_path():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "Relf" / "destination.txt"


def read_destination(setting):
    try:
        destination = Path(setting.read_text(encoding="utf-8-sig").strip())
    except FileNotFoundError:
        return None
    if not destination.is_absolute():
        raise ValueError(f"Use an absolute download folder in {setting}")
    return destination


def default_destination():
    if destination := read_destination(destination_preference_path()):
        return destination
    downloads = Path.home() / "Downloads"
    for name in ("PortableFetch", "ReleaseShelf"):
        legacy = downloads / name
        if legacy.is_dir():
            return legacy
    return downloads / "Relf"


def app_folder_preference_path():
    return destination_preference_path().with_name("app-folder.txt")


def previous_versions_preference_path():
    return destination_preference_path().with_name("previous-versions.txt")


def read_previous_versions(path):
    try:
        value = int(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return DEFAULT_PREVIOUS_VERSIONS
    if value < 0:
        raise ValueError("Enter a non-negative number of previous versions.")
    return value


def validate_app_folder(folder, dest):
    if folder.is_relative_to(dest) or dest.is_relative_to(folder):
        raise ValueError(message("Choose an app folder separate from the download folder: {path}", path=folder))
    return folder


def validate_folder_target(target, dest, settings, others):
    live = target.parent
    backup = previous_app_folder(dest, live)
    protected = (dest, settings, APP_DIR, Path.home().resolve())
    if system_root := os.environ.get("SystemRoot"):
        protected += (Path(system_root).resolve(),)
    if (live == live.parent
            or any(part.is_relative_to(live) or part.is_relative_to(backup) for part in protected)
            or any(other != target and (other.is_relative_to(live) or other.is_relative_to(backup)
                                        or (is_folder_target(other) and target.is_relative_to(other.parent)))
                   for other in others)):
        raise ValueError(message("Choose a dedicated app folder outside Relf storage and other targets: {path}",
                                 path=live))


def load_targets(path, dest):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if path == dest / "targets.json":
            return {}
        raise
    if not isinstance(data, dict):
        raise ValueError("Targets must be an object mapping recipe IDs to absolute file paths")
    targets = {}
    owners = {}
    settings = path.resolve()
    for job, value in data.items():
        key = filename(job).casefold()
        target = Path(value)
        if not target.is_absolute() or not target.name:
            raise ValueError(message("Use an absolute target filename for {job}", job=job))
        filename(target.name)
        folder_target = is_folder_target(target)
        if folder_target:
            check_link(target.parent)
        target = target.parent.resolve() / target.name if folder_target or is_versioned_iso_target(target) else target.resolve()
        if target.is_relative_to(dest) or target == settings:
            raise ValueError(message("Custom targets must be outside the managed download folder and settings file: "
                                     "{target}", target=target))
        if key in targets or target in owners:
            raise ValueError(message("Duplicate recipe ID or target in {path}: {job} -> {target}",
                                     path=path, job=job, target=target))
        targets[key] = target
        owners[target] = job
    for target in owners:
        if is_folder_target(target):
            validate_folder_target(target, dest, settings, owners)
    for target in (*owners, settings):
        if target.suffix.lower() != ".iso" or is_versioned_iso_target(target):
            continue
        prefix, _, version = target.stem.rpartition(" ")
        if prefix and ISO_VERSION.fullmatch(version) and target.with_name(f"{prefix} {{version}}{target.suffix}") in owners:
            raise ValueError(f"Versioned ISO target overlaps a fixed path: {target}")
    return targets


def target_for_job(job, targets, dest, app_folder=None, settings=None):
    keys = [key for key in job.id_keys() if key in targets]
    if len(keys) > 1:
        raise ValueError(message("Multiple target mappings for {app}: {keys}. "
                                 "Keep one mapping for this item in the targets file.",
                                 app=job.id, keys=", ".join(keys)))
    if keys:
        return targets[keys[0]]
    if app_folder is not None:
        folder = validate_app_folder(app_folder, dest) / job.cache_root(dest).name
        check_link(folder)
        target = folder / "{filename}"
        if target in targets.values():
            raise ValueError(message("Choose a dedicated app folder outside Relf storage and other targets: {path}",
                                     path=folder))
        validate_folder_target(target, dest, settings or dest / "targets.json", targets.values())
        return target
    return None


def run_job(action, recipe, dest, target, on_progress=None, *, prepared=None, defer_download=False,
            previous_versions=0, update_app_path=None):
    root = recipe.cache_root(dest)
    if action == "verify":
        return verify(root, recipe, target)
    if action == "resume":
        return resume_publication(root, recipe, target, previous_versions=previous_versions,
                                  update_app_path=update_app_path)
    with fetch_lock(root) if action == "fetch" else nullcontext():
        state = read_state(root, "current.json")
        local = state["version"] if state else "-"
        if prepared is not None and prepared.can_reuse_release(root, state):
            release = prepared.release
        else:
            try:
                release, = publisher_items(lambda: (recipe.resolve(),))
            except ERRORS as exc:
                check_canceled()
                return Result("Source error", local, detail=error_detail(exc))
        try:
            result = run_release(action, root, recipe, release, target, on_progress, defer_download=defer_download,
                                 previous_versions=previous_versions, update_app_path=update_app_path)
            if isinstance(result, Release):
                pending = read_state(root, "pending.json")
                generations = tuple(entry["release"] if entry else None for entry in (state, pending))
                return PreparedDownload(recipe, dest, target, release, generations, previous_versions)
            return result
        except ERRORS as exc:
            return Result("Error", local, release.version, error_detail(exc))


def exit_code(action, result, batch):
    if result.status in {"Source error", "Error"} or (action == "verify" and result.status not in {"Verified", "Local only"}):
        return 1
    if result.status == "Pending" or (batch and result.status in {"Repair needed", "Update available"}):
        return 2
    return 0


def add_recipes_argument(parser):
    parser.add_argument("--recipes", type=Path, default=DEFAULT_CATALOG, help="folder containing recipe files")
    parser.add_argument("--catalog", dest="recipes", type=Path, help=argparse.SUPPRESS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "fetch", "verify"])
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("recipe", type=Path, nargs="?")
    selection.add_argument("--all", action="store_true", help="run every JSON recipe in the recipe folder")
    add_recipes_argument(parser)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--targets", type=Path, help="machine-local output paths (default: DEST/targets.json)")
    args = parser.parse_args(argv)
    dest = (args.dest or default_destination()).resolve()
    targets_path = args.targets or dest / "targets.json"
    targets = load_targets(targets_path, dest)
    app_folder = read_destination(app_folder_preference_path()) if args.dest is None else None
    previous_versions = (read_previous_versions(previous_versions_preference_path())
                         if args.dest is None else DEFAULT_PREVIOUS_VERSIONS)
    if app_folder is not None:
        app_folder = validate_app_folder(app_folder.resolve(), dest)
    paths = sorted(args.recipes.glob("*.json")) if args.all else [args.recipe]
    if not paths:
        raise ValueError(f"No JSON recipes in {args.recipes}")
    jobs = read_jobs(paths)
    codes = []
    if args.all:
        print("Recipe\tLocal\tAvailable\tResult\tDetails")
        jobs = [(path, job) for path, job in jobs if not isinstance(job, (Checklist, LocalItem))
                and (args.action == "verify" or not updates_disabled(job))]

    def process(item):
        _, job = item
        delays = UPDATE_RETRY_DELAYS if args.all and args.action == "fetch" else ()
        return execute(args.action, job, dest, targets, retry_delays=delays, app_folder=app_folder,
                       targets_path=targets_path, previous_versions=previous_versions)

    github_pause_announced = False

    def show_github_pause(_item):
        nonlocal github_pause_announced
        if not github_pause_announced:
            print(GITHUB_PAUSED, file=sys.stderr, flush=True)
            github_pause_announced = True

    results = batch_map(args.action, process, jobs, on_github_pause=show_github_pause)
    for (path, job), result in zip(jobs, results, strict=True):
        name = path.name if isinstance(job, Result) else job.id
        if args.all:
            detail = " | ".join(result.detail.splitlines())
            print(f"{name}\t{result.local}\t{result.available}\t{result.status}\t{detail}")
        else:
            version = result.available if result.available != "-" else result.local
            label = "Source Error" if result.status == "Source error" else result.display_status
            print(f"{label}: {version}\n{result.detail}")
        codes.append(exit_code(args.action, result, args.all))
    return 1 if 1 in codes else max(codes, default=0)


class ChecklistLoadError(Result):
    """An invalid entry whose JSON still identifies it as a checklist."""


def read_jobs(paths):
    jobs = []
    for path in paths:
        data = None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            jobs.append((path, catalog_item(data, path=path)))
        except ERRORS as exc:
            kind = ChecklistLoadError if isinstance(data, dict) and data.get("type") == "checklist" else Result
            jobs.append((path, kind("Error", detail=error_detail(exc))))
    counts = Counter(key for _, recipe in jobs if not isinstance(recipe, Result) for key in recipe.id_keys())
    shortcuts = Counter(recipe.shortcut.casefold() for _, recipe in jobs if isinstance(recipe, Recipe) and recipe.shortcut)
    for index, (path, recipe) in enumerate(jobs):
        if not isinstance(recipe, Result):
            duplicates = [key for key in recipe.id_keys() if counts[key] > 1]
            if duplicates:
                detail = message("Duplicate recipe ID {ids} in {path}", ids=", ".join(duplicates), path=path)
                kind = ChecklistLoadError if isinstance(recipe, Checklist) else Result
                jobs[index] = (path, kind("Error", detail=detail))
            elif isinstance(recipe, Recipe) and recipe.shortcut and shortcuts[recipe.shortcut.casefold()] > 1:
                jobs[index] = (path, Result("Error", detail=f"Duplicate Start menu shortcut: {recipe.shortcut}"))
    return jobs


@dataclass(frozen=True)
class PreparedDownload:
    job: Recipe
    dest: Path
    target: Path | None
    release: Release
    generations: tuple
    previous_versions: int = 0
    retry_delays: tuple = ()

    def can_reuse_release(self, root, current):
        # A competing fetch may have published or journaled a newer release while this one waited.
        pending = read_state(root, "pending.json")
        for old_id, entry in zip(self.generations, (current, pending)):
            if entry and entry["release"] != old_id and not matches(entry, self.job, self.release):
                return False
        return True

    def run(self, on_progress=None, *, cancel_event=None, on_retry=None, update_app_path=None):
        try:
            with cancel_scope(cancel_event):
                return fetch_with_retries(self.job, self.dest, self.target, on_progress, cancel_event,
                                          self.retry_delays, prepared=self, on_retry=on_retry,
                                          previous_versions=self.previous_versions, update_app_path=update_app_path)
        except JobCanceled:
            return Result("Canceled")
        except ERRORS as exc:
            return Result("Error", detail=error_detail(exc))


def fetch_with_retries(job, dest, target, on_progress, cancel_event, delays, *,
                       prepared=None, defer_download=False, on_retry=None, previous_versions=0, update_app_path=None):
    while True:
        check_canceled()
        try:
            result = run_job("fetch", job, dest, target, on_progress, prepared=prepared, defer_download=defer_download,
                             previous_versions=previous_versions, update_app_path=update_app_path)
        except ERRORS as exc:
            result = Result("Error", detail=error_detail(exc))
        if isinstance(result, PreparedDownload):
            return replace(result, retry_delays=delays)
        if result.status not in {"Source error", "Error"} or not delays:
            return result
        delay, delays = delays[0], delays[1:]
        if on_retry is not None:
            on_retry()
        if cancel_event is None:
            sleep(delay)
        else:
            cancel_event.wait(delay)
        # Refresh the publisher URL in case the previous download link expired.
        prepared = None


def prepare_fetch(job, dest, targets, *, cancel_event=None, retry_delays=(), on_retry=None,
                  app_folder=None, targets_path=None, previous_versions=0, update_app_path=None):
    """Complete local work now, or queue the resolved release for a network download."""
    return execute("fetch", job, dest, targets, cancel_event=cancel_event,
                   defer_download=True, retry_delays=retry_delays, on_retry=on_retry, app_folder=app_folder,
                   targets_path=targets_path, previous_versions=previous_versions, update_app_path=update_app_path)


def execute(action, job, dest, targets, on_progress=None, cancel_event=None, *,
            defer_download=False, retry_delays=(), on_retry=None, app_folder=None, targets_path=None,
            previous_versions=0, update_app_path=None):
    try:
        with cancel_scope(cancel_event):
            if isinstance(job, Result):
                return job
            if isinstance(job, Checklist):
                return Result("Error", detail="Open the setup checklist in Relf to choose adjustments.")
            if isinstance(job, LocalItem):
                return local_result(job)
            if action in ("check", "fetch", "resume") and job.updates_disabled:
                return Result("Disabled")
            target = target_for_job(job, targets, dest, app_folder, targets_path)
            if action == "fetch":
                return fetch_with_retries(job, dest, target, on_progress, cancel_event, retry_delays,
                                          defer_download=defer_download, on_retry=on_retry,
                                          previous_versions=previous_versions, update_app_path=update_app_path)
            return run_job(action, job, dest, target, on_progress, previous_versions=previous_versions,
                           update_app_path=update_app_path)
    except JobCanceled:
        return Result("Canceled")
    except ERRORS as exc:
        return Result("Error", detail=error_detail(exc))


def local_result(job):
    if job.path.exists():
        return Result("Local only", detail=str(job.path))
    return Result("Error", detail=message("Missing file: {path}", path=job.path))


def run_cli():
    try:
        sys.exit(main())
    except ERRORS as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    run_cli()
