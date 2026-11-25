"""Exercise the CLI with publisher fixtures or an optional loopback server."""

import hashlib
import io
import json
import os
import runpy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from email.message import Message
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
import stat
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.response import addinfourl
import zipfile

from fetch_sources import Recipe, copy_with_progress, open_url, windows_product_version
from fetch_store import DOWNGRADE_SKIPPED, Result, digest, version_is_newer, write_state
from releaseshelf import batch_map, default_destination, execute
from shelf_cancel import cancel_scope, publisher_items
from shelf_theme import preference_path


PROJECT = Path(__file__).resolve().parent


class AdaptiveBatchTest(unittest.TestCase):
    def test_batch_grows_slows_and_recovers_with_ordered_results(self):
        starts = Queue()
        finish = [Event() for _ in range(27)]
        errors = {
            13: HTTPError("https://publisher.example", 429, "Too Many Requests", Message(), None),
            17: HTTPError("https://publisher.example", 503, "Service Unavailable", Message(), None),
            21: URLError(TimeoutError("Publisher timed out")),
        }
        fail = {index: Event() for index in errors}
        reported = {index: Event() for index in errors}

        def worker(index):
            starts.put(index)
            if index in errors:
                self.assertTrue(fail[index].wait(10))

                def read():
                    raise errors[index]

                with cancel_scope(Event()), self.assertRaises(URLError):
                    list(publisher_items(read))
                reported[index].set()
            self.assertTrue(finish[index].wait(10))
            return Result("Latest", detail=str(index))

        def expect_starts(*indices):
            self.assertCountEqual([starts.get(timeout=5) for _ in indices], indices)

        def expect_no_start():
            with self.assertRaises(Empty):
                starts.get(timeout=0.1)

        def report(index):
            fail[index].set()
            self.assertTrue(reported[index].wait(5))

        with ThreadPoolExecutor(max_workers=1) as runner:
            results = runner.submit(list, batch_map("fetch", worker, range(len(finish))))
            try:
                expect_starts(0, 1, 2)
                expect_no_start()
                # Keep the first result blocked while the scheduler refills in completion order.
                next_start = 3
                for index in range(1, 13):
                    finish[index].set()
                    new_slots = 2 if index in (3, 6, 9) else 1
                    expect_starts(*range(next_start, next_start + new_slots))
                    next_start += new_slots
                expect_no_start()

                report(13)
                for index in (0, 14, 15):
                    finish[index].set()
                expect_no_start()
                finish[16].set()
                expect_starts(18)

                report(17)
                finish[13].set()
                finish[17].set()
                expect_no_start()
                finish[18].set()
                expect_starts(19)
                for index in (19, 20):
                    finish[index].set()
                    expect_starts(index + 1)
                report(21)
                for index in (21, 22, 23):
                    finish[index].set()
                    expect_starts(index + 1)
                    expect_no_start()
                finish[24].set()
                expect_starts(25, 26)
            finally:
                for event in (*fail.values(), *finish):
                    event.set()
            self.assertEqual([result.detail for result in results.result(timeout=5)],
                             [str(index) for index in range(len(finish))])


class Publisher(SimpleHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        super().do_GET()

    def log_message(self, *args):
        pass


class RelfTest(unittest.TestCase):
    def test_renamed_storage_preserves_existing_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            downloads = home / "Downloads"
            downloads.mkdir()
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, LOCALAPPDATA=temp):
                self.assertEqual(default_destination(), downloads / "Relf")
                self.assertEqual(preference_path(), home / "Relf" / "appearance.txt")
                for name in ("ReleaseShelf", "PortableFetch"):
                    (downloads / name).mkdir()
                    self.assertEqual(default_destination(), downloads / name)
                (home / "ReleaseShelf").mkdir()
                self.assertEqual(preference_path(), home / "ReleaseShelf" / "appearance.txt")

    def test_configured_destination_overrides_legacy_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "Downloads" / "PortableFetch").mkdir(parents=True)
            settings = home / "Relf"
            settings.mkdir()
            destination = home / "My Drive" / "Software" / "Relf"
            setting = settings / "destination.txt"
            setting.write_text(str(destination) + "\n", encoding="utf-8-sig")
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, LOCALAPPDATA=temp):
                self.assertEqual(default_destination(), destination)
                setting.write_text("relative", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "absolute download folder"):
                    default_destination()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.publisher = base / "publisher"
        self.publisher.mkdir()
        self.dest = base / "downloads"
        self.requests = []
        self.use_http = os.environ.get("RELEASESHELF_HTTP_TEST", os.environ.get("PORTABLE_FETCH_HTTP_TEST")) == "1"
        url = "https://publisher.example"
        if self.use_http:
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Publisher, directory=self.publisher))
            server.requests = self.requests
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}"
        self.recipe = {
            "id": "sample-tool", "previous_ids": ["Example.SampleTool"],
            "category": "Utilities", "description": "Test utility.",
            "page": url + "/page.html", "version_pattern": r"SampleTool v([0-9.]+)",
            "download": url + "/sample-tool.zip", "extract": ["SampleTool.exe", "help.txt", "notes.txt"],
        }
        self.recipe_file = base / "recipe.json"
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.page = self.publisher / "page.html"
        self.page.write_text("<h1>SampleTool v1.27</h1>")
        self.archive = self.publisher / "sample-tool.zip"
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, ("fixture:" + name).encode())
        self.job = self.dest / self.recipe["id"]
        self.manifest = self.job / "current.json"

    def reply(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.requests.append(path)
        data = (self.publisher / path.lstrip("/")).read_bytes()
        headers = Message()
        headers["Content-Length"] = str(len(data))
        headers["Last-Modified"] = "Tue, 10 Nov 2026 16:20:57 GMT"
        return addinfourl(io.BytesIO(data), headers, request.full_url, 200)

    def test_local_catalog_item_is_offline_and_skipped_in_batches(self):
        folder = self.recipe_file.parent / "catalog"
        folder.mkdir()
        self.recipe_file = folder / "local.json"
        self.recipe_file.write_text(json.dumps({"id": "portable", "local_path": "../publisher"}))
        for action in ("check", "fetch", "verify"):
            self.assertIn("Local only", self.cli(action))
            self.assertNotIn("portable", self.cli(action, batch=folder))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.dest.exists())

    def test_disabled_recipe_skips_publisher_and_allows_verification(self):
        self.recipe_file.write_text(json.dumps({**self.recipe, "updates_disabled": True}))
        for action in ("check", "fetch"):
            self.assertIn("Disabled", self.cli(action))
            self.assertNotIn(self.recipe["id"], self.cli(action, batch=self.recipe_file.parent))
        self.assertIn(self.recipe["id"], self.cli("verify", batch=self.recipe_file.parent, expected_code=1))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.dest.exists())

    def test_direct_download_uses_remote_modification_time(self):
        url = self.recipe["download"]
        job = Recipe.from_data({"id": "fixed", "direct_download": url})
        with patch("urllib.request.OpenerDirector.open", side_effect=self.reply) as request:
            release = job.resolve()
        self.assertEqual(request.call_args.args[0].get_method(), "HEAD")
        self.assertEqual((release.version, release.download), ("2026-11-10 16:20:57 UTC", url))

    def test_direct_url_version_detects_repack_and_rejects_changed_transfer(self):
        url = "https://publisher.example/installer-1.2.exe"
        job = Recipe.from_data({"id": "setup", "direct_download": url,
                                "direct_version": r"url:/installer-([0-9.]+)\.exe$"})
        stamp = "Tue, 10 Nov 2026 16:20:57 GMT"
        transfer_stamp = stamp

        def reply(_url, *, method=None):
            headers = Message()
            headers["Last-Modified"] = stamp if method == "HEAD" else transfer_stamp
            return addinfourl(io.BytesIO(b"setup"), headers, url, 200)

        with patch("fetch_sources.open_url", side_effect=reply):
            self.assertEqual(execute("fetch", job, self.dest, {}).local, "1.2")
            self.assertEqual(execute("check", job, self.dest, {}).status, "Latest")
            stamp = "Wed, 11 Nov 2026 16:20:57 GMT"
            self.assertEqual(execute("check", job, self.dest, {}).status, "Update available")
            self.assertEqual(execute("fetch", job, self.dest, {}).status, "Error")
            transfer_stamp = stamp
            self.assertEqual(execute("fetch", job, self.dest, {}).status, "Updated")

    @unittest.skipUnless(os.name == "nt", "Uses a real Windows executable")
    def test_direct_version_uses_bounded_installer_metadata_reads(self):
        exe = Path(sys.executable)
        url = "https://publisher.example/setup.exe"
        template = {"id": "setup", "direct_download": url}
        for source, payload, expected in (
                ("file_version", exe.read_bytes(), windows_product_version(exe)),
                ("qt_installer", bytes(80000) + b"<Installer><Name>Pro</Name><Version>4.7</Version></Installer>", "4.7")):
            job = Recipe.from_data({**template, "direct_version": source})
            counts = []

            def reply(_url, *, method=None, headers=None):
                meta = Message()
                meta["Last-Modified"] = "Tue, 10 Nov 2026 16:20:57 GMT"
                if method == "HEAD":
                    meta["Content-Length"] = str(len(payload))
                    return addinfourl(io.BytesIO(), meta, url, 200)
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                counts.append(end - start + 1)
                meta["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
                return addinfourl(io.BytesIO(payload[start:end + 1]), meta, url, 206)

            with self.subTest(source=source), patch("fetch_sources.open_url", side_effect=reply):
                self.assertEqual(job.resolve().version, expected)
                self.assertLess(sum(counts), len(payload))

    @unittest.skipUnless(os.name == "nt", "Uses a real Windows executable")
    def test_direct_version_uses_one_full_response_when_ranges_are_ignored(self):
        exe = Path(sys.executable)
        payload = exe.read_bytes()
        url = "https://publisher.example/setup.exe"
        stamp = "Tue, 10 Nov 2026 16:20:57 GMT"
        job = Recipe.from_data({"id": "setup", "direct_download": url, "direct_version": "file_version"})

        def reply(_url, *, method=None, headers=None):
            meta = Message()
            if method == "HEAD":
                meta["Last-Modified"] = stamp
                meta["Content-Length"] = str(len(payload))
                return addinfourl(io.BytesIO(), meta, url, 200)
            self.assertEqual(headers, {"Range": "bytes=0-63", "If-Unmodified-Since": stamp})
            meta["Last-Modified"] = transfer_stamp
            return addinfourl(io.BytesIO(body), meta, url, 200)

        transfer_stamp, body = stamp, payload
        with patch("fetch_sources.open_url", side_effect=reply) as request:
            self.assertEqual(job.resolve().version, windows_product_version(exe))
            self.assertEqual(request.call_count, 2)
            body = payload[:-1]
            with self.assertRaisesRegex(ValueError, "Installer size changed"):
                job.resolve()
            transfer_stamp, body = "Wed, 11 Nov 2026 16:20:57 GMT", payload
            with self.assertRaisesRegex(ValueError, "requested installer metadata"):
                job.resolve()

    def test_github_rate_limit_parks_only_github_jobs_until_reset(self):
        now = 1000
        jobs = [Recipe.from_data({"id": f"github-{index}", "github": f"owner/tool-{index}",
                                  "asset_pattern": "setup.exe"}) for index in range(4)]
        jobs.append(Recipe.from_data(self.recipe))
        cancel = {job.id: Event() for job in jobs}
        paused, finished = Queue(), Queue()
        requests = []

        def reply(request, timeout):
            if request.host != "api.github.com":
                requests.append("publisher")
                return self.reply(request, timeout)
            path = urlsplit(request.full_url).path
            requests.append(path)
            if now < 1060:
                meta = Message()
                meta["X-RateLimit-Remaining"] = "0"
                meta["X-RateLimit-Reset"] = "1060"
                raise HTTPError(request.full_url, 403, "rate limit exceeded", meta, None)
            data = {"tag_name": "v1", "assets": [{"name": "setup.exe",
                    "browser_download_url": "https://publisher.example/setup.exe"}]}
            return addinfourl(io.BytesIO(json.dumps(data).encode()), Message(), request.full_url, 200)

        def check(job):
            result = execute("check", job, self.dest, {}, cancel_event=cancel[job.id])
            finished.put((job.id, result.status))
            return result

        with (patch("fetch_sources._github_reset", 0), patch("fetch_sources.time", side_effect=lambda: now),
              patch("releaseshelf.START_PARALLEL_ITEMS", 1),
              patch("urllib.request.OpenerDirector.open", side_effect=reply),
              ThreadPoolExecutor(max_workers=1) as runner):
            results = runner.submit(list, batch_map("check", check, jobs, on_github_pause=paused.put,
                                                    is_canceled=lambda job: cancel[job.id].is_set()))
            try:
                self.assertEqual([paused.get(timeout=5) for _ in range(4)], jobs[:4])
                self.assertEqual(finished.get(timeout=5), (jobs[4].id, "Update available"))
                self.assertEqual(requests, ["/repos/owner/tool-0/releases/latest", "publisher"])
                cancel[jobs[1].id].set()
                self.assertEqual(finished.get(timeout=5), (jobs[1].id, "Canceled"))
            finally:
                now = 1060
            self.assertEqual([result.status for result in results.result(timeout=5)],
                             ["Update available", "Canceled", "Update available", "Update available", "Update available"])
            self.assertEqual(requests[2:], [f"/repos/owner/tool-{index}/releases/latest" for index in (0, 2, 3)])

    def test_single_github_request_honors_retry_after_and_preserves_plain_403(self):
        now = 1000
        url = "https://api.github.com/repos/owner/tool/releases/latest"
        headers = Message()
        headers["Retry-After"] = "15"
        soft_limit = HTTPError(url, 403, "secondary rate limit", headers, None)
        forbidden = HTTPError(url, 403, "Forbidden", {}, None)

        def wait(seconds):
            nonlocal now
            now += seconds

        with (patch("fetch_sources._github_reset", 0), patch("fetch_sources.time", side_effect=lambda: now),
              patch("fetch_sources.wait_cancellable", side_effect=wait) as sleeper,
              patch("urllib.request.OpenerDirector.open", side_effect=[soft_limit, forbidden]) as request):
            with self.assertRaises(HTTPError) as failure:
                open_url(url)
            self.assertIs(failure.exception, forbidden)
            sleeper.assert_called_once_with(15)
            self.assertEqual(request.call_count, 2)

    def test_version_and_build_tokens_come_from_same_page_match(self):
        data = {**self.recipe, "version_pattern": r"v([0-9.]+) build (?P<asset>\d+)",
                "download": "https://publisher.example/{version}/setup-{asset}.exe"}
        with patch("fetch_sources.read_page", return_value="v1.27 build 41"):
            result = Recipe.from_data(data).resolve()
        self.assertEqual(result.download, "https://publisher.example/1.27/setup-41.exe")

    @unittest.skipUnless(os.name == "nt", "Uses Windows tar for source ZIPs")
    def test_github_commit_snapshot_switches_to_release_and_follows_tags(self):
        api = "https://api.github.com/repos/owner/tool"
        source = "https://codeload.github.com/owner/tool/zip/"
        commits = ["a" * 40, "b" * 40, "c" * 40]
        recipe = Recipe.from_data({
            "id": "sample-tool", "page": api + "/commits/main",
            "version_pattern": r"date=(\S+) sha=(?P<asset>[a-f0-9]{40})",
            "download": source + "{asset}", "filename": "tool-{asset}.zip",
            "extract_folder": "tool-{asset}", "extract": ["Install.cmd"],
        })
        live = self.job / "current" / "Install.cmd"
        tag = None

        def lookup(url):
            if url == api + "/releases/latest":
                if tag is None:
                    raise HTTPError(url, 404, "Not Found", {}, None)
                return json.dumps({"tag_name": tag, "assets": []})
            if tag is None:
                self.assertEqual(url, recipe.page)
                return "date=2026-11-13 sha=" + commits[0]
            self.assertEqual(url, api + "/commits/refs%2Ftags%2F" + tag)
            return json.dumps({"sha": commit})

        def archive(url):
            self.assertEqual(url, source + commit)
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as package:
                package.writestr("tool-" + commit + "/Install.cmd", commit)
            data.seek(0)
            return addinfourl(data, Message(), url)

        with (patch("fetch_sources.read_page", side_effect=lookup),
              patch("fetch_sources.open_url", side_effect=archive) as downloads):
            for tag, commit in zip((None, "v1.0", "v1.1"), commits):
                version = tag or "2026-11-13"
                checked = execute("check", recipe, self.dest, {})
                self.assertEqual((checked.status, checked.available), ("Update available", version))
                updated = execute("fetch", recipe, self.dest, {})
                self.assertEqual((updated.status, updated.local), ("Updated", version), updated.detail)
                self.assertEqual(live.read_text(), commit)
                self.assertEqual(execute("check", recipe, self.dest, {}).status, "Latest")
                self.assertEqual(execute("verify", recipe, self.dest, {}).local, version)
            self.assertEqual(downloads.call_count, 3)

    def test_direct_github_branch_zip_checks_releases_and_preserves_lookup_errors(self):
        url = "https://github.com/owner/tool/archive/refs/heads/main.zip"
        recipe = Recipe.from_data({"id": "tool", "direct_download": url, "extract_folder": "tool-main",
                                   "extract": ["Install.cmd"]})
        sha = "1" * 40
        with patch("fetch_sources.read_page", side_effect=[json.dumps({"tag_name": "v2"}), json.dumps({"sha": sha})]):
            release = recipe.resolve()
        self.assertEqual((release.version, release.source_folder), ("v2", "tool-" + sha))
        with (patch("fetch_sources.read_page", side_effect=HTTPError(url, 403, "Forbidden", {}, None)),
              patch("fetch_sources.open_url") as download):
            with self.assertRaises(HTTPError):
                recipe.resolve()
            download.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Uses Windows tar")
    def test_distinct_builds_with_same_version_publish_the_correct_folder(self):
        self.recipe_file.write_text(json.dumps({
            **self.recipe, "version_pattern": r"v([0-9.]+) build (?P<asset>\d+)",
            "download": self.recipe["download"].replace("sample-tool", "build-{asset}"),
            "filename": "build-{asset}.zip", "extract_folder": "build-{asset}",
            "extract": ["tool.exe"],
        }))
        for build in ("41", "42"):
            self.page.write_text(f"v1.27 build {build}")
            with zipfile.ZipFile(self.publisher / f"build-{build}.zip", "w") as package:
                package.writestr(f"build-{build}/tool.exe", build)
            self.assertIn("Update available: 1.27", self.cli("check"))
            self.assertIn("Updated: 1.27", self.cli("fetch"))
            self.assertEqual((self.job / "current/tool.exe").read_text(), build)
            self.assertIn("Latest: 1.27", self.cli("check"))
            self.assertIn("Verified: 1.27", self.cli("verify"))

    def test_publisher_confirmation_download(self):
        from fetch_sources import Release, download_chunks
        url = "https://publisher.ucoz.ru/tool.exe"
        release = Release("1", url, "tool.exe", None)
        recipe = Recipe.from_data(self.recipe)
        form = Message()
        form["Content-Type"] = "text/html"
        responses = [addinfourl(io.BytesIO(b'<input type="hidden" name="ses" value="x+/=">'), form, url),
                     addinfourl(io.BytesIO(b"archive"), Message(), url)]
        with patch("fetch_sources.open_url", side_effect=responses) as request:
            self.assertEqual(list(download_chunks(recipe, release)), [None, b"archive"])
        request.assert_called_with(url, data=b"ses=x%2B%2F%3D")

    def test_majorgeeks_download_follows_current_mirror_link(self):
        from fetch_sources import Release, download_chunks
        page = "https://www.majorgeeks.com/mg/getmirror/tool,1.html"
        url = "https://files3.majorgeeks.com/token/downloads/tool.zip"
        release = Release("1", page, "tool.zip", None)
        recipe = Recipe.from_data(self.recipe)
        with patch("fetch_sources.read_page", return_value=f"<!-- Debug: {url} -->") as landing, \
             patch("fetch_sources.open_url", return_value=addinfourl(io.BytesIO(b"zip"), Message(), url)) as request:
            self.assertEqual(list(download_chunks(recipe, release)), [None, b"zip"])
            landing.assert_called_once_with(page)
            request.assert_called_once_with(url)
        with patch("fetch_sources.read_page", return_value="<!-- Debug: https://unrelated.example/tool.zip -->"):
            with self.assertRaisesRegex(ValueError, "download link"):
                list(download_chunks(recipe, release))

    def test_versioned_member_tracks_new_filename(self):
        self.recipe["extract"] = ["Tool {version}.exe"]
        self.recipe_file.write_text(json.dumps(self.recipe))
        for version in ("1.27", "1.28"):
            self.page.write_text(f"SampleTool v{version}")
            with zipfile.ZipFile(self.archive, "w") as package:
                package.writestr(f"Tool {version}.exe", version.encode())
            self.assertIn("Updated", self.cli("fetch"))
            self.assertIn("Verified", self.cli("verify"))
            self.assertEqual((self.job / "current" / f"Tool {version}.exe").read_text(), version)
        self.assertFalse((self.job / "current" / "Tool 1.27.exe").exists())

    def cli(self, action, *, success=True, batch=None, expected_code=None):
        selection = ["--all", "--catalog", str(batch)] if batch else [str(self.recipe_file)]
        args = [str(PROJECT / "relf.py"), action, *selection, "--dest", str(self.dest)]
        if self.use_http:
            result = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=15)
            if expected_code is not None:
                self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
                return result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            return result.stdout + result.stderr
        output = io.StringIO()
        with (patch("urllib.request.OpenerDirector.open", side_effect=self.reply),
              patch.object(sys, "argv", args), redirect_stdout(output), redirect_stderr(output),
              self.assertRaises(SystemExit) as result):
            runpy.run_path(args[0], run_name="__main__")
        if expected_code is not None:
            self.assertEqual(result.exception.code, expected_code, output.getvalue())
            return output.getvalue()
        self.assertEqual(result.exception.code == 0, success, str(result.exception))
        return output.getvalue() + str(result.exception)

    @unittest.skipUnless(os.name == "nt", "Uses Windows tar and Shell shortcuts")
    def test_folder_publication_and_shortcut_repair(self):
        recipe = Recipe.from_data({**self.recipe, "extract_folder": "Tool v{version}",
                                   "extract": ["tool.exe"], "shortcut": "Tool"})
        link = Path(self.temp.name) / "Programs" / "Tool.lnk"
        live = self.job / "current"

        def package(version, files):
            self.page.write_text(f"SampleTool v{version}")
            with zipfile.ZipFile(self.archive, "w") as z:
                for name, contents in files.items():
                    z.writestr(f"Tool v{version}/{name}", contents)

        def run(action):
            return execute(action, recipe, self.dest, {})

        with (patch("urllib.request.OpenerDirector.open", side_effect=self.reply),
              patch("fetch_store.shortcut_path", return_value=link)):
            package("1.0", {"tool.exe": b"exe", "settings/default.ini": b"first", "old.txt": b"old"})
            self.assertEqual(run("check").status, "Update available")
            self.assertFalse(link.exists())
            result = run("fetch")
            self.assertEqual(result.status, "Updated", result.detail)
            self.assertEqual(run("verify").status, "Verified")
            (live / "user.log").write_bytes(b"keep")
            (live / "settings/default.ini").unlink()
            self.assertEqual(run("verify").status, "Repair needed")
            self.assertEqual(run("fetch").status, "Cached")
            self.assertEqual((live / "settings/default.ini").read_bytes(), b"first")
            link.unlink()
            self.assertEqual(run("check").status, "Repair needed")
            with patch("fetch_store.write_shortcut", side_effect=PermissionError(13, "Access denied", str(link), 5)):
                self.assertEqual(run("fetch").display_status, "Pending")
            self.assertEqual(run("fetch").status, "Cached")
            self.assertTrue(link.is_file())
            self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
            package("1.1", {"tool.exe": b"exe", "settings/default.ini": b"second"})
            self.assertEqual(run("fetch").status, "Updated")
            self.assertFalse((live / "old.txt").exists())
            self.assertEqual((live / "settings/default.ini").read_bytes(), b"second")
            self.assertEqual((live / "user.log").read_bytes(), b"keep")
            self.assertFalse((self.job / ".previous-app").exists())
            package("1.2", {"tool.exe": b"changed", "../outside": b"invalid"})
            self.assertEqual(run("fetch").status, "Error")
            self.assertEqual((live / "tool.exe").read_bytes(), b"exe")
            self.assertEqual(run("verify").status, "Verified")

    def test_companions_survive_updates_and_restore_from_a_moved_catalog(self):
        self.recipe.update(extract=["SampleTool.exe", "help.txt"])
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        seeds = Recipe.read(self.recipe_file).companion_dir
        (seeds / "Settings").mkdir(parents=True)
        (seeds / "Settings/profile.ini").write_text("portable")
        (seeds / "help.txt").write_text("imported")
        live = self.job / "current"
        self.assertIn("Repair needed", self.cli("verify", success=False))
        self.assertIn("Cached", self.cli("fetch"))
        self.assertEqual((live / "help.txt").read_text(), "imported")
        (live / "help.txt").write_text("edited by app")
        self.page.write_text("SampleTool v1.28")
        self.cli("fetch")
        self.assertEqual((live / "help.txt").read_text(), "edited by app")
        self.assertIn("Verified", self.cli("verify"))
        (live / "Settings/profile.ini").unlink()
        self.assertIn("Cached", self.cli("fetch"))
        self.assertEqual((live / "Settings/profile.ini").read_text(), "portable")
        catalog = Path(self.temp.name) / "moved"
        catalog.mkdir()
        seeds.parent.rename(catalog / "companions")
        self.recipe_file = self.recipe_file.rename(catalog / self.recipe_file.name)
        self.dest = catalog / "fresh"
        self.cli("fetch")
        self.assertEqual((self.dest / self.recipe["id"] / "current/help.txt").read_text(), "imported")

    def test_companions_with_custom_file_target(self):
        seeds = Recipe.read(self.recipe_file).companion_dir
        seeds.mkdir(parents=True)
        (seeds / "tool.ini").write_text("seed")
        target = Path(self.temp.name) / "install/renamed.exe"
        self.dest.mkdir()
        (self.dest / "targets.json").write_text(json.dumps({self.recipe["id"]: str(target)}))
        self.cli("fetch")
        config = target.with_name("tool.ini")
        self.assertFalse(config.exists())
        self.recipe["apply_custom_settings"] = True
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Cached", self.cli("fetch"))
        self.assertEqual(config.read_text(), "seed")
        config.write_text("edited")
        self.assertIn("Apply custom settings", self.cli("verify", success=False))
        self.assertIn("Cached", self.cli("fetch"))
        self.assertEqual(config.read_text(), "seed")
        self.recipe["apply_custom_settings"] = False
        self.recipe_file.write_text(json.dumps(self.recipe))
        config.write_text("edited")
        self.assertIn("Verified", self.cli("verify"))
        (seeds / "renamed.exe").write_text("conflict")
        self.assertIn("conflicts", self.cli("fetch", success=False))
        self.assertEqual(config.read_text(), "edited")

    def test_settings_in_managed_app_with_publisher_default(self):
        self.recipe["extract"].append("prefs.xml")
        with zipfile.ZipFile(self.archive, "a") as package:
            package.writestr("prefs.xml", "publisher")
        self.recipe["settings_files"] = ["prefs.xml"]
        self.recipe_file.write_text(json.dumps(self.recipe))
        files = Recipe.read(self.recipe_file).companion_dir
        files.mkdir(parents=True)
        (files / "prefs.xml").write_text("custom")
        (files / "restore.ps1").write_text("support")
        self.cli("fetch")
        live = self.job / "current"
        self.assertEqual((live / "prefs.xml").read_text(), "publisher")
        (live / "restore.ps1").write_text("working copy")
        self.recipe["apply_custom_settings"] = True
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Cached", self.cli("fetch"))
        self.assertEqual((live / "prefs.xml").read_text(), "custom")
        self.assertEqual((live / "restore.ps1").read_text(), "working copy")
        self.assertIn("Verified", self.cli("verify"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_fetch_repeat_and_bad_update(self):
        self.assertIn(f"Missing manifest: {self.manifest}", self.cli("check"))
        self.assertFalse(self.dest.exists())
        self.assertIn("Updated: 1.27", self.cli("fetch"))
        saved = self.manifest.read_bytes()
        state = json.loads(saved)
        folder = self.job / "releases" / state["release"]
        original = self.archive.read_bytes()
        self.assertEqual((folder / self.archive.name).read_bytes(), original)
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            for name in self.recipe["extract"]:
                self.assertEqual((folder / "files" / name).read_bytes(), package.read(name))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        self.page.write_text("SampleTool v1.28")
        self.archive.write_bytes(b"incomplete zip")
        self.assertIn("Error:", self.cli("fetch", success=False))
        self.assertEqual(self.manifest.read_bytes(), saved)
        for name, checksum in state["sha256"].items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), checksum)
        self.assertEqual(list(self.job.glob(".stage-*")), [])
        self.assertEqual(list((self.job / "releases").iterdir()), [folder])

    def test_fetch_reports_progress_only_for_network_transfer(self):
        job = Recipe.read(self.recipe_file)
        payload = self.archive.read_bytes()
        for known_length in (True, False):
            with self.subTest(known_length=known_length):
                destination = self.dest / str(known_length)
                response = io.BytesIO(payload)
                response.headers = {"Content-Length": str(len(payload))} if known_length else {}
                progress = []
                with (
                    patch("fetch_sources.read_page", return_value="SampleTool v1.27"),
                    patch("fetch_sources.open_url", return_value=response) as network,
                    patch("fetch_sources.zipfile.ZipFile", wraps=zipfile.ZipFile) as validate,
                ):
                    def report(received, total):
                        self.assertFalse(validate.called)
                        progress.append((received, total))

                    result = execute("fetch", job, destination, {}, report)
                    self.assertEqual(result.status, "Updated")
                    total = len(payload) if known_length else None
                    self.assertEqual(progress, [(0, total), (len(payload), total), (None, None)])
                    self.assertTrue(validate.called)
                    progress.clear()
                    self.assertEqual(execute("fetch", job, destination, {}, report).status, "Latest")
                    published = Path(json.loads((destination / job.id / "current.json").read_text())["published"]["path"])
                    published.unlink()
                    self.assertEqual(execute("fetch", job, destination, {}, report).status, "Cached")
                    self.assertEqual(execute("check", job, destination, {}, report).status, "Latest")
                    self.assertEqual(execute("verify", job, destination, {}, report).status, "Verified")
                    self.assertEqual(progress, [])
                    network.assert_called_once()

    def test_fast_download_progress_is_throttled_and_finishes_at_actual_size(self):
        payload = b"x" * (4 * 1024 * 1024)
        progress = []
        with patch("fetch_sources.monotonic", side_effect=[1, 1.01, 1.02, 1.03]):
            target = io.BytesIO()
            copy_with_progress(io.BytesIO(payload), target, len(payload),
                               lambda received, total: progress.append((received, total)))
        self.assertEqual(target.getvalue(), payload)
        self.assertEqual(progress, [(0, len(payload)), (1024 * 1024, len(payload)),
                                    (len(payload), len(payload)), (None, None)])

    def test_slow_download_reports_available_bytes_before_buffer_fills(self):
        response = io.BytesIO(b"download")
        progress = []
        with (
            patch.object(response, "read1", side_effect=lambda size: response.read(2)),
            patch("fetch_sources.monotonic", side_effect=[1, 1.2, 1.4, 1.6]),
        ):
            copy_with_progress(response, io.BytesIO(), None,
                               lambda received, total: progress.append((received, total)))
        self.assertEqual(progress, [(0, None), (2, None), (4, None), (6, None),
                                    (8, None), (None, None)])

    def test_failed_download_ends_progress_and_cleans_staging(self):
        job = Recipe.read(self.recipe_file)
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted):
                destination = self.dest / str(interrupted)
                response = io.BytesIO(b"partial")
                response.headers = {"Content-Length": "100"}
                reads = [b"partial", OSError("Transfer interrupted") if interrupted else b""]
                progress = []
                with (
                    patch("fetch_sources.read_page", return_value="SampleTool v1.27"),
                    patch("fetch_sources.open_url", return_value=response),
                    patch.object(response, "read1", side_effect=reads),
                ):
                    result = execute("fetch", job, destination, {},
                                     lambda received, total: progress.append((received, total)))
                self.assertEqual(result.status, "Error")
                self.assertEqual(progress, [(0, 100), (7, 100), (None, None)])
                root = destination / job.id
                self.assertFalse((root / "current.json").exists())
                self.assertEqual(list(root.glob(".stage-*")), [])
                self.assertEqual(list((root / "releases").iterdir()), [])

    def test_cancel_stalled_publisher_reads_releases_the_job(self):
        job = Recipe.read(self.recipe_file)
        for action in ("check", "fetch"):
            with self.subTest(action=action):
                waiting, resume, closed, cancel = Event(), Event(), Event(), Event()
                response = io.BytesIO(b"partial")
                response.headers = {"Content-Length": "100"}

                def read(size):
                    waiting.set()
                    resume.wait(5)
                    return b""

                def close():
                    io.BytesIO.close(response)
                    closed.set()

                results = []
                progress = []
                worker = Thread(target=lambda: results.append(execute(
                    action, job, self.dest, {}, lambda a, b: progress.append((a, b)), cancel)))
                # A check stalls on its first page; an update stalls after resolving that page.
                version = (patch("fetch_sources.read_page", return_value="SampleTool v1.27")
                           if action == "fetch" else nullcontext())
                with (patch.object(response, "read1", read), patch.object(response, "close", close),
                      patch("fetch_sources.open_url", return_value=response), version):
                    worker.start()
                    try:
                        self.assertTrue(waiting.wait(2))
                        cancel.set()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual([result.status for result in results], ["Canceled"])
                        self.assertFalse(self.manifest.exists())
                        self.assertFalse((self.job / "pending.json").exists())
                        self.assertEqual(list(self.job.glob(".stage-*")), [])
                        if action == "fetch":
                            self.assertEqual(list((self.job / "releases").iterdir()), [])
                    finally:
                        resume.set()
                        worker.join(2)
                        self.assertTrue(closed.wait(2))
                if action == "fetch" and progress:
                    self.assertEqual(progress[-1], (None, None))
        self.assertIn("Updated", self.cli("fetch"))

    def test_cancel_before_or_after_publication_commit(self):
        self.recipe["extract"] = ["SampleTool.exe"]
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        previous = self.manifest.read_bytes()
        state = json.loads(previous)
        target = Path(state["published"]["path"])
        previous_output = target.read_bytes()
        self.page.write_text("SampleTool v1.28")
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, b"replacement")
        job = Recipe.read(self.recipe_file)
        cancel = Event()

        def cancel_before_commit(path):
            checksum = digest(path)
            if path.name.startswith(".releaseshelf-"):
                cancel.set()
            return checksum

        def cancel_after_journal(path, data):
            write_state(path, data)
            if path.name == "pending.json":
                cancel.set()

        with patch("urllib.request.OpenerDirector.open", side_effect=self.reply):
            with patch("fetch_store.digest", side_effect=cancel_before_commit):
                result = execute("fetch", job, self.dest, {}, cancel_event=cancel)
            self.assertEqual(result.status, "Canceled")
            self.assertEqual(self.manifest.read_bytes(), previous)
            self.assertEqual(target.read_bytes(), previous_output)
            self.assertEqual([p.name for p in (self.job / "releases").iterdir()], [state["release"]])
            self.assertFalse((self.job / "pending.json").exists())
            self.assertEqual(list(target.parent.glob(".releaseshelf-*")), [])

            cancel.clear()
            with patch("fetch_store.write_state", side_effect=cancel_after_journal):
                result = execute("fetch", job, self.dest, {}, cancel_event=cancel)
            self.assertEqual(result.status, "Updated")
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(json.loads(self.manifest.read_bytes())["version"], "1.28")
            self.assertFalse((self.job / "pending.json").exists())

    def test_unknown_version_and_missing_file_repair(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        (self.job / "releases" / state["release"] / "files" / "SampleTool.exe").unlink()
        missing = self.job / "releases" / state["release"] / "files" / "SampleTool.exe"
        self.assertIn(f"Missing file: {missing}", self.cli("check"))
        self.assertIn(f"Fetch needed: Missing file: {missing}", self.cli("fetch"))
        self.assertIn("Latest", self.cli("check"))
        self.page.write_text("Version unavailable")
        self.assertIn("unknown", self.cli("check", success=False))
        self.assertIn("unknown", self.cli("fetch", success=False))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    def test_catalog_metadata_keeps_existing_release_current(self):
        self.recipe.pop("category")
        self.recipe.pop("description")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        saved = self.manifest.read_bytes()
        self.recipe.update(category="Hardware", description="Reports battery health.")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Verified: 1.27", self.cli("verify"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(self.manifest.read_bytes(), saved)
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_reject_unsafe_destination(self):
        self.recipe["extract"] = ["../outside.exe"]
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn("Invalid filename", self.cli("fetch", success=False))
        self.assertFalse(self.dest.exists())

    def test_restored_app_id_reuses_previous_cache_manifest_and_custom_target(self):
        current_id = "sample-tool"
        old_id = "Example.SampleTool"
        self.assertEqual(self.recipe["id"], current_id)
        self.recipe_file.write_text(json.dumps(dict(self.recipe, id=old_id, previous_ids=[current_id])))
        target = Path(self.temp.name) / "tools" / "SampleTool.exe"
        self.dest.mkdir()
        mappings = self.dest / "targets.json"
        mappings.write_text(json.dumps({old_id.upper(): str(target)}))
        self.cli("fetch")
        manifest = self.dest / old_id / "current.json"
        saved = manifest.read_bytes()

        self.recipe_file.write_text(json.dumps(self.recipe))
        self.assertIn(f"SHA-256 verified: {target}", self.cli("verify"))
        self.assertIn("Latest: 1.27", self.cli("check"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(manifest.read_bytes(), saved)
        self.assertFalse((self.dest / current_id).exists())

        mappings.write_text(json.dumps({current_id: str(target)}))
        target.unlink()
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertIn(f"SHA-256 verified: {target}", self.cli("verify"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_conflicting_cache_and_target_ids_stop_before_fetch(self):
        current_id = self.recipe["id"]
        old_id, = self.recipe["previous_ids"]
        (self.dest / current_id).mkdir(parents=True)
        (self.dest / old_id).mkdir()
        self.assertIn("Multiple cache paths", self.cli("fetch", expected_code=1))
        (self.dest / old_id).rmdir()
        (self.dest / "targets.json").write_text(json.dumps({
            key: str(Path(self.temp.name) / f"{key}.exe") for key in (old_id, current_id)
        }))
        self.assertIn("Multiple target mappings", self.cli("fetch", expected_code=1))
        self.assertEqual(self.requests, [])

    def test_repeat_checks_explain_changed_file_and_missing_release(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        folder = self.job / "releases" / state["release"]
        archive = folder / self.archive.name
        archive.write_bytes(b"changed")
        self.assertIn(f"SHA-256 mismatch: {archive}", self.cli("check"))
        folder.rename(folder.with_name("moved"))
        self.assertIn(f"Missing release folder: {folder}", self.cli("check"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_archive_only_with_fixed_filename_and_new_version(self):
        self.recipe.pop("extract")
        self.recipe["filename"] = "SampleTool-32bit.zip"
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        previous = json.loads(self.manifest.read_text())["release"]
        self.page.write_text("SampleTool v1.28")
        self.assertIn("Updated: 1.28", self.cli("fetch"))
        state = json.loads(self.manifest.read_text())
        self.assertNotEqual(previous, state["release"])
        folder = self.job / "releases" / state["release"]
        self.assertEqual(list(folder.parent.iterdir()), [folder])
        self.assertEqual([p.name for p in folder.iterdir()], [self.recipe["filename"]])
        self.assertIn("Latest: 1.28", self.cli("check"))

    def test_fetch_latest_removes_legacy_cache_history(self):
        self.cli("fetch")
        releases = self.job / "releases"
        stale = releases / ("a" * 32)
        previous = self.job / ".previous-app"
        personal = releases / "personal"
        for folder in (stale, previous, personal):
            folder.mkdir()
            (folder / "file.txt").write_text("fixture")
        if os.name == "nt":
            (stale / "file.txt").chmod(stat.S_IREAD)
            stale.chmod(stat.S_IREAD)
        self.cli("check")
        self.cli("verify")
        self.assertTrue(stale.is_dir())
        self.assertTrue(previous.is_dir())
        self.assertIn("Latest", self.cli("fetch"))
        self.assertFalse(stale.exists())
        self.assertFalse(previous.exists())
        self.assertTrue((personal / "file.txt").is_file())
        self.assertIn("Verified", self.cli("verify"))
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_versioned_publication_removes_only_unchanged_managed_files(self):
        self.recipe.pop("extract")
        self.recipe["filename"] = "Tool-{version}.zip"
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.cli("fetch")
        live = self.job / "current"
        legacy = self.job / "releases" / ("a" * 32)
        legacy.mkdir()
        (legacy / "Tool-1.26.zip").write_bytes(b"older archive")
        (live / "Tool-1.26.zip").write_bytes(b"older archive")
        self.assertIn("Latest", self.cli("fetch"))
        self.assertFalse((live / "Tool-1.26.zip").exists())
        self.page.write_text("SampleTool v1.28")
        self.cli("fetch")
        self.assertFalse((live / "Tool-1.27.zip").exists())
        previous = live / "Tool-1.28.zip"
        previous.write_bytes(b"personal changes")
        self.page.write_text("SampleTool v1.29")
        self.cli("fetch")
        self.assertEqual(previous.read_bytes(), b"personal changes")
        self.assertEqual((live / "Tool-1.29.zip").read_bytes(), self.archive.read_bytes())

    def test_versioned_iso_and_reissued_publisher_checksum(self):
        base_url = self.recipe["page"].rsplit("/", 1)[0]
        self.recipe_file.write_text(json.dumps({
            "id": "live-image", "page": self.recipe["page"], "version_pattern": r"version=(\S+)",
            "download": base_url + "/images/{version}/live-{version}.iso",
            "filename": "Live {version}.iso", "sha256_pattern": r"sha256=(\S+)",
        }))
        image = self.publisher / "images" / "1.0" / "live-1.0.iso"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"first image")
        self.page.write_text("version=1.0 sha256=" + hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertIn("Updated: 1.0", self.cli("fetch"))
        self.assertEqual(self.requests.count("/images/1.0/live-1.0.iso"), 1)
        published = self.dest / "live-image" / "current" / "Live 1.0.iso"
        self.assertEqual(published.read_bytes(), b"first image")
        self.assertIn("Latest: 1.0", self.cli("check"))

        replacement = b"reissued image"
        self.page.write_text("version=1.0 sha256=" + hashlib.sha256(replacement).hexdigest())
        self.assertIn("Update available: 1.0", self.cli("check"))
        self.assertIn("SHA-256 mismatch", self.cli("fetch", success=False))
        self.assertEqual(published.read_bytes(), b"first image")
        image.write_bytes(replacement)
        self.assertIn("Updated: 1.0", self.cli("fetch"))
        self.assertEqual(published.read_bytes(), replacement)
        self.assertIn("Verified: 1.0", self.cli("verify"))
        self.page.write_text("version=1.0 sha256=unfinished")
        self.assertIn("Invalid SHA-256", self.cli("fetch", success=False))
        self.assertEqual(self.requests.count("/images/1.0/live-1.0.iso"), 3)

    def test_versioned_iso_target_retires_only_its_unchanged_previous_file(self):
        self.recipe.pop("extract")
        self.recipe["download"] = self.recipe["page"].replace("page.html", "live.iso")
        self.recipe_file.write_text(json.dumps(self.recipe))
        image = self.publisher / "live.iso"
        image.write_bytes(b"first image")
        folder = self.dest.parent / "images"
        folder.mkdir()
        self.dest.mkdir()
        targets = self.dest / "targets.json"
        template = folder / "Live {version}.iso"
        previous, current = (folder / f"Live {version}.iso" for version in ("1.27", "1.28"))
        targets.write_text(json.dumps({self.recipe["id"]: str(template), "other": str(previous)}))
        self.assertIn("Versioned ISO target overlaps", self.cli("fetch", success=False))
        targets.write_text(json.dumps({self.recipe["id"]: str(template)}))
        self.assertIn("Updated: 1.27", self.cli("fetch"))
        self.assertIn(f"SHA-256 verified: {previous}", self.cli("verify"))

        self.page.write_text("SampleTool v1.28")
        image.write_bytes(b"second image")
        current.write_bytes(b"unrelated file")
        self.assertIn("A different file already exists", self.cli("fetch", expected_code=1))
        self.assertEqual(current.read_bytes(), b"unrelated file")
        self.assertTrue(previous.is_file())
        current.unlink()
        self.assertIn("Cached: 1.28", self.cli("fetch"))
        self.assertFalse(previous.exists())
        self.assertIn(f"SHA-256 verified: {current}", self.cli("verify"))

        current.write_bytes(b"personal changes")
        self.page.write_text("SampleTool v1.29")
        self.assertIn("Updated: 1.29", self.cli("fetch"))
        self.assertEqual(current.read_bytes(), b"personal changes")
        self.assertEqual((folder / "Live 1.29.iso").read_bytes(), b"second image")

    def test_expanded_archive_name_is_validated_before_download(self):
        recipe = Recipe.from_data({
            "id": "live-image", "page": self.recipe["page"], "version_pattern": r"version=(\S+)",
            "download": "https://publisher.example/latest.iso", "filename": "{version}.iso",
        })
        with patch("fetch_sources.read_page", return_value="version=../outside"):
            with self.assertRaises(ValueError):
                recipe.resolve()

    def test_windows_link_is_requested_only_for_an_actual_download(self):
        recipe = Recipe.from_data({
            "id": "test-image", "page": "https://www.microsoft.com/en-us/software-download/windows11",
            "version_pattern": r"version=(\S+)", "sha256_pattern": r"sha256=(\S+)",
            "download_resolver": "windows-11-english-x64", "filename": "image-{version}.iso",
        })
        payload = b"Windows installation media fixture"
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        checksum = hashlib.sha256(payload).hexdigest()
        page = "version=26H2 sha256=" + checksum
        with (patch("fetch_sources.read_page", return_value=page),
              patch("fetch_sources.open_windows_iso", return_value=response) as open_iso,
              patch("fetch_sources.open_url") as ordinary_download):
            self.assertEqual(execute("check", recipe, self.dest, {}).status, "Update available")
            open_iso.assert_not_called()
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Updated")
            open_iso.assert_called_once_with("26H2", checksum, "")
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Latest")
            self.assertEqual(execute("verify", recipe, self.dest, {}).status, "Verified")
            state = json.loads((self.dest / recipe.id / "current.json").read_text())
            published = Path(state["published"]["path"])
            self.assertEqual(published.read_bytes(), payload)
            published.unlink()
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Cached")
            open_iso.assert_called_once()
            ordinary_download.assert_not_called()

    def test_windows_iso_mirror_requires_a_matching_publisher_checksum(self):
        data = {
            "id": "test-image", "page": "https://www.microsoft.com/en-us/software-download/windows11",
            "version_pattern": r"version=(\S+)", "sha256_pattern": r"sha256=(\S+)",
            "download_resolver": "windows-11-english-x64", "filename": "image-{version}.iso",
        }
        payload = b"retail ISO fixture"
        checksum = hashlib.sha256(payload).hexdigest()
        mirror = "https://mirror.example/retail.iso"
        recipe = Recipe.from_data({**data, "iso_mirror": {"url": mirror, "sha256": checksum}})
        self.assertEqual(recipe.fingerprint(), Recipe.from_data(data).fingerprint())
        with patch("fetch_sources.read_page", return_value=f"version=26H2 sha256={'0' * 64}"):
            self.assertEqual(recipe.resolve().download, "")

        wrong = io.BytesIO(b"wrong ISO")
        wrong.headers = {}
        valid = io.BytesIO(payload)
        valid.headers = {"Content-Length": str(len(payload))}
        with (patch("fetch_sources.read_page", return_value=f"version=26H2 sha256={checksum}"),
              patch("fetch_sources.open_url", side_effect=[wrong, valid]) as download,
              patch("shelf_windows_iso.windows_iso_url") as microsoft):
            self.assertEqual(execute("check", recipe, self.dest, {}).status, "Update available")
            download.assert_not_called()
            rejected = execute("fetch", recipe, self.dest, {})
            self.assertIn("SHA-256 mismatch", rejected.detail)
            self.assertFalse((self.dest / recipe.id / "current.json").exists())
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Updated")
            self.assertEqual(execute("verify", recipe, self.dest, {}).status, "Verified")
            self.assertEqual([call.args for call in download.call_args_list], [(mirror,), (mirror,)])
            microsoft.assert_not_called()

    def test_windows_iso_uses_microsoft_if_the_mirror_is_unavailable(self):
        from shelf_windows_iso import open_windows_iso

        mirror = "https://mirror.example/retail.iso"
        unavailable = HTTPError(mirror, 503, "Unavailable", Message(), io.BytesIO())
        url = "https://software.download.prss.microsoft.com/retail.iso"
        with (patch("fetch_sources.open_url", side_effect=unavailable),
              patch("shelf_windows_iso.windows_iso_url", return_value=url),
              patch("shelf_windows_iso.build_opener") as opener):
            opener.return_value.open.return_value = io.BytesIO(b"Microsoft ISO")
            with open_windows_iso("26H2", "a" * 64, mirror) as response:
                self.assertEqual(response.read(), b"Microsoft ISO")
            self.assertEqual(opener.return_value.open.call_args.args[0].full_url, url)
            self.assertTrue(unavailable.fp.closed)

    def test_adopt_original_manifest_and_repair_live_file_without_downloading(self):
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        live = Path(state["published"]["path"])
        original = live.read_bytes()
        live.unlink()
        old_fields = ("version", "release", "recipe", "sha256")
        self.manifest.write_text(json.dumps({key: state[key] for key in old_fields}))
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), original)
        live.write_bytes(b"stale portable application")
        self.assertIn("Repair needed", self.cli("check"))
        self.assertIn("Cached: 1.27", self.cli("fetch"))
        self.assertIn("Latest: 1.27", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), original)
        self.assertEqual(json.loads(self.manifest.read_text())["release"], state["release"])
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    def test_quick_check_and_offline_full_verification(self):
        self.cli("fetch")
        live = Path(json.loads(self.manifest.read_text())["published"]["path"])
        info = live.stat()
        changed = bytearray(live.read_bytes())
        changed[0] ^= 1
        live.write_bytes(changed)
        os.utime(live, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertIn("Latest: 1.27", self.cli("check"))
        before = list(self.requests)
        self.assertIn("SHA-256 mismatch", self.cli("verify", expected_code=1))
        self.assertEqual(self.requests, before)
        self.cli("fetch")
        self.assertIn("Verified: 1.27", self.cli("verify"))

    def test_fetch_hashes_an_archive_once_per_stage(self):
        data = self.archive.read_bytes()
        raw = {**self.recipe, "sha256": hashlib.sha256(data).hexdigest()}
        del raw["extract"]
        recipe = Recipe.from_data(raw)
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data))}
        with (patch("fetch_sources.read_page", return_value="SampleTool v1.27"),
              patch("fetch_sources.open_url", return_value=response) as publisher,
              patch("fetch_sources.digest", wraps=digest) as downloaded_hash,
              patch("fetch_store.digest", wraps=digest) as local_hash):
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Updated")
            downloaded_hash.assert_called_once()
            local_hash.assert_called_once()  # Read back the staged publication.
            state = json.loads(self.manifest.read_text())
            self.assertEqual(state["sha256"][recipe.archive], raw["sha256"])

            Path(state["published"]["path"]).unlink()
            local_hash.reset_mock()
            self.assertEqual(execute("fetch", recipe, self.dest, {}).status, "Cached")
            self.assertEqual(local_hash.call_count, 2)  # Cached archive and staged publication.
            publisher.assert_called_once()
            self.assertEqual(execute("verify", recipe, self.dest, {}).status, "Verified")

    def test_version_precedence(self):
        for newer, older in (("v1.10", "1.9"), ("1.2.3+build", "1.2.3-rc.10"),
                             ("1.2.3-rc.10", "1.2.3-rc.2"),
                             ("2026-11-25 12:00:00 UTC", "2026-11-24 12:00:00 UTC")):
            with self.subTest(newer=newer, older=older):
                self.assertTrue(version_is_newer(newer, older))
                self.assertFalse(version_is_newer(older, newer))
        self.assertFalse(version_is_newer("1.2.0.0", "v1.2+build"))
        self.assertFalse(version_is_newer("2026-11-25 12:00:00 UTC", "1.2"))
        self.assertFalse(version_is_newer("2026-11-25", "v1.0"))

    def test_older_publisher_release_cannot_replace_a_managed_version(self):
        self.page.write_text("SampleTool v1.30")
        self.cli("fetch")
        state = self.manifest.read_bytes()
        target = Path(json.loads(state)["published"]["path"])
        installed = target.read_bytes()
        self.page.write_text("SampleTool v1.27")
        for action in ("check", "fetch"):
            self.assertIn(DOWNGRADE_SKIPPED, self.cli(action))
        self.assertEqual(target.read_bytes(), installed)
        self.assertEqual(self.manifest.read_bytes(), state)
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)

    @unittest.skipUnless(os.name == "nt", "Windows version resource")
    def test_newer_external_executable_survives_cached_repair_and_download(self):
        recipe = dict(self.recipe, download=self.recipe["download"].replace("sample-tool.zip", "SampleTool.exe"))
        del recipe["extract"]
        self.recipe_file.write_text(json.dumps(recipe))
        package = self.publisher / "SampleTool.exe"
        executable = Path(sys.executable)
        installed = executable.read_bytes()
        version = windows_product_version(executable)
        older = "1" + version[1:]
        package.write_bytes(installed.replace(version.encode("utf-16le"), older.encode("utf-16le")))
        live = self.publisher.parent / "installed" / "SampleTool.exe"
        self.dest.mkdir()
        (self.dest / "targets.json").write_text(json.dumps({recipe["id"]: str(live)}))
        self.cli("fetch")
        self.assertEqual(windows_product_version(live), older)
        state = self.manifest.read_bytes()
        live.write_bytes(installed)
        for action in ("check", "verify", "fetch"):
            with self.subTest(action=action):
                self.assertIn(DOWNGRADE_SKIPPED, self.cli(action, expected_code=1 if action == "verify" else 0))
                self.assertEqual(live.read_bytes(), installed)
                self.assertEqual(self.manifest.read_bytes(), state)
        self.assertEqual(self.requests.count("/SampleTool.exe"), 1)

        self.page.write_text("SampleTool v1.28")
        self.assertIn(DOWNGRADE_SKIPPED, self.cli("fetch"))
        self.assertEqual(live.read_bytes(), installed)
        self.assertEqual(self.manifest.read_bytes(), state)
        self.assertFalse((self.job / "pending.json").exists())
        self.assertEqual(self.requests.count("/SampleTool.exe"), 2)
        self.assertEqual([path.name for path in (self.job / "releases").iterdir()], [json.loads(state)["release"]])

        live.write_bytes(package.read_bytes())
        package.write_bytes(package.read_bytes() + b"new build of the old version")

        def external_update_during_publication(path, data):
            write_state(path, data)
            if path.name == "pending.json":
                live.write_bytes(installed)

        with patch("fetch_store.write_state", side_effect=external_update_during_publication):
            self.assertIn(DOWNGRADE_SKIPPED, self.cli("fetch"))
        self.assertEqual(live.read_bytes(), installed)
        self.assertEqual(self.manifest.read_bytes(), state)
        self.assertFalse((self.job / "pending.json").exists())

        updated = installed.replace(version.encode("utf-16le"), ("9" + version[1:]).encode("utf-16le"))
        package.write_bytes(updated)
        self.assertIn("Updated", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), updated)

    @unittest.skipUnless(os.name == "nt", "Windows version resources and sharing semantics")
    def test_queued_portable_folder_keeps_a_newer_executable_installed_while_waiting(self):
        executable = Path(sys.executable)
        installed = executable.read_bytes()
        version = windows_product_version(executable)
        candidate = installed.replace(version.encode("utf-16le"), ("1" + version[1:]).encode("utf-16le"))
        self.cli("fetch")
        state = self.manifest.read_bytes()
        target = Path(json.loads(state)["published"]["path"])
        self.page.write_text("SampleTool v1.28")
        with zipfile.ZipFile(self.archive, "w") as package:
            package.writestr(target.name, candidate)
            package.writestr("help.txt", "older help")
            package.writestr("notes.txt", "older notes")
        with target.open("rb"):
            self.assertIn("Waiting for the app to close", self.cli("fetch", expected_code=2))
        target.write_bytes(installed)
        (target.parent / "help.txt").write_text("newer help")
        recipe = Recipe.read(self.recipe_file)
        result = execute("resume", recipe, self.dest, {})
        self.assertEqual((result.status, result.local, result.available), (DOWNGRADE_SKIPPED, version, "1.28"))
        self.assertEqual(target.read_bytes(), installed)
        self.assertEqual((target.parent / "help.txt").read_text(), "newer help")
        self.assertEqual(self.manifest.read_bytes(), state)
        self.assertFalse((self.job / "pending.json").exists())
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_locked_custom_target_retries_cached_release(self):
        live = Path(self.temp.name) / "installed" / "my-tool.exe"
        live.parent.mkdir()
        self.dest.mkdir()
        (self.dest / "targets.json").write_text(json.dumps({self.recipe["id"]: str(live)}))
        self.cli("fetch")
        old = live.read_bytes()
        old_state = self.manifest.read_bytes()
        self.page.write_text("SampleTool v1.28")
        with zipfile.ZipFile(self.archive, "w") as package:
            for name in self.recipe["extract"]:
                package.writestr(name, "updated")
        with live.open("rb"):
            self.assertIn("Waiting for the app to close", self.cli("fetch", expected_code=2))
            self.assertEqual(live.read_bytes(), old)
            self.assertEqual(self.manifest.read_bytes(), old_state)
            self.assertIn("Waiting for the app to close", self.cli("check", expected_code=2))
            self.assertIn("Waiting for the app to close", self.cli("verify", expected_code=1))
        self.assertIn("Cached: 1.28", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), b"updated")
        self.assertFalse((self.job / "pending.json").exists())
        self.assertEqual([p.name for p in (self.job / "releases").iterdir()],
                         [json.loads(self.manifest.read_text())["release"]])
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    @unittest.skipUnless(os.name == "nt", "Windows executable sharing semantics")
    def test_running_portable_app_keeps_its_folder_until_it_exits(self):
        with zipfile.ZipFile(self.archive, "w") as package:
            package.write(Path(os.environ["SystemRoot"]) / "System32/ping.exe", "SampleTool.exe")
            package.writestr("help.txt", "help")
            package.writestr("notes.txt", "notes")
        self.cli("fetch")
        live = self.job / "current" / "SampleTool.exe"
        with subprocess.Popen([str(live), "127.0.0.1", "-n", "15"], creationflags=subprocess.CREATE_NO_WINDOW,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as app:
            try:
                self.assertIsNone(app.poll())
                self.page.write_text("SampleTool v1.28")
                with zipfile.ZipFile(self.archive, "w") as package:
                    for name in self.recipe["extract"]:
                        package.writestr(name, "updated")
                self.assertIn("Waiting for the app to close", self.cli("fetch", expected_code=2))
                self.assertEqual((live.parent / "help.txt").read_text(), "help")
                self.assertFalse((self.job / ".previous-app").exists())
            finally:
                app.terminate()
        self.assertIn("Cached: 1.28", self.cli("fetch"))
        self.assertEqual(live.read_bytes(), b"updated")
        self.assertEqual(self.requests.count("/sample-tool.zip"), 2)

    @unittest.skipUnless(os.name == "nt", "Windows version resource")
    def test_publish_exe_rejects_stale_executable(self):
        self.recipe.update(version_pattern=r"<b>([\d.]+)</b>\s*\[",
                           extract=["sample.exe"], publish_exe="sample.exe")
        self.recipe_file.write_text(json.dumps(self.recipe))
        # Windows Python supplies an actual PE resource without a publisher download.
        exe = Path(sys.executable)
        version = windows_product_version(exe)
        self.page.write_text(f"Latest release notes <b>{version}</b> [release date]")
        with zipfile.ZipFile(self.archive, "w") as package:
            package.write(exe, "sample.exe")
        self.cli("fetch")
        manifest = self.job / "current.json"
        previous = manifest.read_bytes()
        self.page.write_text("Latest release notes <b>99.9.9.9</b> [release date]")
        result = self.cli("fetch", expected_code=1)
        self.assertIn(f"expected 99.9.9.9, got {version}", result)
        self.assertEqual(manifest.read_bytes(), previous)
        self.assertEqual((manifest.parent / "bin" / "sample.exe").read_bytes(), exe.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows version resource")
    def test_legacy_publish_exe_uses_named_member_and_original_cache(self):
        exe = Path(sys.executable)
        version = windows_product_version(exe)
        self.recipe.update(extract=["notes.txt", "portable.exe"], publish_exe="portable.exe")
        self.recipe_file.write_text(json.dumps(self.recipe))
        self.page.write_text(f"SampleTool v{version}")
        with zipfile.ZipFile(self.archive, "w") as package:
            package.writestr("notes.txt", "release notes")
            package.write(exe, "portable.exe")
        self.cli("fetch")
        state = json.loads(self.manifest.read_text())
        legacy_fields = {key: self.recipe[key] for key in
                         ("id", "page", "version_pattern", "download", "extract", "publish_exe")}
        legacy_fields["archive"] = self.archive.name
        legacy_hash = hashlib.sha256(json.dumps(legacy_fields, sort_keys=True).encode()).hexdigest()
        self.assertEqual(state["recipe"], legacy_hash)
        old = {key: state[key] for key in ("version", "release", "recipe", "sha256")}
        self.manifest.write_text(json.dumps(old))
        live = self.job / "bin" / "portable.exe"
        live.write_bytes(b"older installed executable")
        self.assertIn("Cached", self.cli("fetch"))
        self.assertIn("Latest", self.cli("fetch"))
        self.assertIn("Verified", self.cli("verify"))
        self.assertEqual(live.read_bytes(), exe.read_bytes())
        self.assertEqual(json.loads(self.manifest.read_text())["release"], old["release"])
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        self.assertFalse((self.job / "current").exists())

    def test_batch_isolates_failures_and_returns_actionable_exit_codes(self):
        catalog = Path(self.temp.name) / "catalog"
        catalog.mkdir()
        (catalog / "good.json").write_text(self.recipe_file.read_text())
        self.assertIn("Update available", self.cli("check", batch=catalog, expected_code=2))
        (catalog / "broken.json").write_text("{")
        result = self.cli("fetch", batch=catalog, expected_code=1)
        self.assertIn("broken.json", result)
        self.assertIn("Updated", result)
        (catalog / "broken.json").unlink()
        self.assertIn("Latest", self.cli("check", batch=catalog, expected_code=0))
        self.page.write_text("no version")
        self.assertIn("1.27\t-\tSource error", self.cli("check", batch=catalog, expected_code=1))
        self.assertIn("Verified", self.cli("verify", batch=catalog, expected_code=0))

    def test_duplicate_recipe_ids_are_rejected_before_fetch(self):
        catalog = Path(self.temp.name) / "catalog"
        catalog.mkdir()
        for name in ("first", "second"):
            (catalog / (name + ".json")).write_text(self.recipe_file.read_text())
        self.assertEqual(self.cli("fetch", batch=catalog, expected_code=1).count("Duplicate recipe ID"), 2)
        self.assertFalse(self.dest.exists())

    def test_github_release_filename_checksum_and_ambiguous_selection(self):
        if self.use_http:
            self.skipTest("GitHub API fixture uses the in-process transport")
        api = self.publisher / "repos" / "owner" / "tool" / "releases" / "latest"
        api.parent.mkdir(parents=True)
        self.recipe_file.write_text(json.dumps({"id": "github-tool", "github": "owner/tool",
                                                "asset_pattern": "*-win.zip"}))
        name = "Tool-v1.0-win.zip"
        asset = {"name": name, "browser_download_url": self.recipe["download"],
                 "digest": "sha256:" + hashlib.sha256(self.archive.read_bytes()).hexdigest()}
        release = {"tag_name": "v1.0", "assets": [asset]}
        api.write_text(json.dumps(release))
        self.cli("fetch")
        live = self.dest / "github-tool" / "current" / name
        self.assertEqual(live.read_bytes(), self.archive.read_bytes())
        self.assertIn("Latest", self.cli("check"))
        release["assets"].append(dict(asset, name="Tool-arm-win.zip"))
        api.write_text(json.dumps(release))
        result = self.cli("fetch", expected_code=1)
        self.assertIn(name, result)
        self.assertIn("Tool-arm-win.zip", result)
        self.assertEqual(self.requests.count("/sample-tool.zip"), 1)
        release["assets"].pop()
        asset["digest"] = "sha256:" + "0" * 64
        api.write_text(json.dumps(release))
        self.assertIn("Publisher SHA-256 mismatch", self.cli("fetch", expected_code=1))
        self.assertEqual(live.read_bytes(), self.archive.read_bytes())


if __name__ == "__main__":
    unittest.main()
