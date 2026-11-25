"""Exercise release validation and the Windows executable replacement."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from shelf_update import (LATEST_RELEASE, RELEASE_DOWNLOAD, RelfUpdate, check_for_update,
                          download_update, prepare_update, start_update_helper)


class UpdateTest(unittest.TestCase):
    def test_only_newer_stable_release_with_verified_executable_is_offered(self):
        checksum = "a" * 64
        release = {"tag_name": "v0.10.0", "body": "- Release notes",
                   "assets": [{"name": "Relf.exe", "state": "uploaded", "size": 123,
                               "digest": f"sha256:{checksum}"}]}
        with (patch("shelf_update.__version__", "0.9.0"),
              patch("shelf_update.read_page", return_value=json.dumps(release)) as page):
            self.assertEqual(check_for_update(), RelfUpdate("0.10.0", "- Release notes", 123, checksum))
            page.assert_called_once_with(LATEST_RELEASE)
            for fields in ({"tag_name": "v0.8.0"}, {"tag_name": "v0.9.0"}, {"prerelease": True}, {"draft": True}):
                page.return_value = json.dumps(release | fields)
                self.assertIsNone(check_for_update())
            for asset in ({"name": "anything.exe"}, {"state": "new"}, {"digest": None}, {"digest": "sha256:abcd"}):
                page.return_value = json.dumps(release | {"assets": [release["assets"][0] | asset]})
                with self.assertRaises(ValueError):
                    check_for_update()

    def test_download_checks_sha256_and_executable_version(self):
        payload = b"new relf"
        release = RelfUpdate("0.10.0", "", len(payload), hashlib.sha256(payload).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            with (patch("shelf_update.open_url", return_value=io.BytesIO(payload)) as download,
                  patch("shelf_update.windows_product_version", return_value="0.10.0")):
                download_update(release, stage / "verified.exe")
                download.assert_called_once_with(f"{RELEASE_DOWNLOAD}/v0.10.0/Relf.exe")
                self.assertEqual((stage / "verified.exe").read_bytes(), payload)
            with (patch("shelf_update.open_url", return_value=io.BytesIO(b"BAD RELF")),
                  self.assertRaisesRegex(ValueError, "SHA-256")):
                prepare_update(stage / "Relf.exe", release)
            self.assertEqual(list(stage.iterdir()), [stage / "verified.exe"])
            with (patch("shelf_update.open_url", return_value=io.BytesIO(payload)),
                  patch("shelf_update.windows_product_version", return_value="0.9.0"),
                  self.assertRaisesRegex(ValueError, "version does not match")):
                download_update(release, stage / "wrong-version.exe")

    @unittest.skipUnless(os.name == "nt", "Windows replacement helper")
    def test_helper_waits_then_restarts_or_restores_the_previous_executable(self):
        system = Path(os.environ["SystemRoot"]) / "System32"
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory(prefix="Relf update ") as tmp:
                folder = Path(tmp)
                stage = folder / ".relf-update-test"
                stage.mkdir()
                executable = folder / "Relf.exe"
                staged = stage / "Relf.exe"
                script = folder / "record launch.cmd"
                restored = folder / "restored.txt"
                script.write_text(
                    '@echo off\n'
                    'if defined RELF_UPDATE_READY type nul > "%RELF_UPDATE_READY%"\n'
                    f'if defined RELF_UPDATE_FAILURE type nul > "{restored}"\n', encoding="utf-8",
                )
                cmd, ping = system / "cmd.exe", system / "ping.exe"
                shutil.copyfile(cmd if rollback else ping, executable)
                if rollback:
                    staged.write_bytes(b"Not a Windows executable")
                else:
                    shutil.copyfile(cmd, staged)
                old_hash = hashlib.sha256(executable.read_bytes()).digest()
                expected_hash = old_hash if rollback else hashlib.sha256(staged.read_bytes()).digest()
                running = ping if rollback else executable
                with subprocess.Popen([str(running), "127.0.0.1", "-n", "20"], stdout=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW) as old:
                    try:
                        with start_update_helper(executable, stage, (old.pid,),
                                                 ["/d", "/c", str(script)]) as helper:
                            try:
                                self.assertIsNone(helper.poll())
                                self.assertEqual(hashlib.sha256(executable.read_bytes()).digest(), old_hash)
                            finally:
                                old.terminate()
                            try:
                                self.assertEqual(helper.wait(timeout=15), int(rollback))
                            except subprocess.TimeoutExpired:
                                helper.kill()
                                raise
                    finally:
                        old.terminate()
                self.assertEqual(hashlib.sha256(executable.read_bytes()).digest(), expected_hash)
                self.assertFalse(stage.exists())
                if rollback:
                    deadline = monotonic() + 5
                    while not restored.exists() and monotonic() < deadline:
                        sleep(.05)
                    self.assertTrue(restored.exists())
