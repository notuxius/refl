"""Obtain the checksum-verified Windows 11 English x64 image on demand."""

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shelf_cancel import check_canceled, report_publisher_backpressure
from shelf_message import message
from shelf_version import __version__


FIDO_SOURCE = "https://raw.githubusercontent.com/pbatard/Fido/master/Fido.ps1"
FIDO_COMMAND = r"""
$ErrorActionPreference = 'Stop'
$path = (Get-Item -LiteralPath .\Fido.ps1).FullName
$signature = Get-AuthenticodeSignature -LiteralPath $path
if ($signature.Status -in @('NotSigned', 'HashMismatch')) {
    # GitHub can deliver LF while the publisher signed the CRLF representation.
    $content = [IO.File]::ReadAllText($path) -replace '\r?\n', "`r`n"
    [IO.File]::WriteAllText($path, $content, (New-Object Text.UTF8Encoding($true)))
    $signature = Get-AuthenticodeSignature -LiteralPath $path
}
if ($signature.Status -ne 'Valid' -or
    $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)CN=Akeo Consulting(?:,|$)') {
    exit 20
}
& $path -Win 'Windows 11' -Rel $env:RELEASESHELF_WINDOWS_RELEASE -Lang '^English$' -Arch x64 -PlatformArch x64 -GetUrl
if ($LASTEXITCODE) { exit $LASTEXITCODE }
"""


def microsoft_iso_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not (parts.hostname or "").endswith(".microsoft.com")
            or parts.username is not None or parts.password is not None
            or parts.port not in (443, None) or not parts.path.lower().endswith(".iso")):
        raise ValueError("Unexpected Microsoft ISO download location.")
    return value


class MicrosoftRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, microsoft_iso_url(newurl))


def windows_iso_url(version):
    """Request a fresh signed link without fetching any image bytes."""
    from fetch_sources import open_url

    if os.name != "nt" or not re.fullmatch(r"\d{2}H[12]", version, re.ASCII):
        raise ValueError("Windows ISO link generation requires Windows and a recognized release.")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    check_canceled()
    with open_url(FIDO_SOURCE) as response:
        script = response.read(2_000_001)
        if not script or len(script) > 2_000_000:
            raise ValueError("Invalid Fido download.")
    check_canceled()
    with tempfile.TemporaryDirectory(prefix="releaseshelf-fido-") as folder:
        (Path(folder) / "Fido.ps1").write_bytes(script)
        with tempfile.TemporaryFile(dir=folder) as log:
            with subprocess.Popen(
                [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                 "-Command", FIDO_COMMAND], cwd=folder,
                env={**os.environ, "RELEASESHELF_WINDOWS_RELEASE": version,
                     "PSModulePath": str(powershell.parent / "Modules")},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ) as process:
                deadline = monotonic() + 150
                try:
                    while True:
                        check_canceled()
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            raise ValueError("Timed out requesting a Microsoft ISO download link.")
                        try:
                            code = process.wait(timeout=min(remaining, 0.2))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process.poll() is None:
                        process.kill()
            log.seek(0)
            output = log.read().decode("utf-8", errors="replace")
    check_canceled()
    if code == 20:
        raise ValueError("Fido publisher verification failed.")
    if code:
        if "Sentinel marked this request as rejected" in output:
            raise ValueError("Microsoft rejected the Windows ISO link request (Sentinel).")
        refusal = re.search(r"\b\d{3}-\d{6}\b", output)
        if refusal:
            raise ValueError(message("Microsoft refused the ISO download link (code {code}).", code=refusal[0]))
        raise ValueError(message("Fido could not obtain a Microsoft ISO download link (exit {code}).",
                                 code=code))
    links = [line.strip() for line in output.splitlines() if line.strip().startswith("https://")]
    if len(links) != 1:
        raise ValueError("Microsoft did not issue a single ISO download link.")
    return microsoft_iso_url(links[0])


@contextmanager
def open_windows_iso(version, checksum, mirror=""):
    """Stream a checksum-pinned mirror or request a link from Microsoft."""
    from fetch_sources import open_url

    check_canceled()
    if not isinstance(checksum, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", checksum):
        raise ValueError("Microsoft did not publish a valid English x64 ISO checksum.")
    response = None
    if mirror:
        try:
            response = open_url(mirror)
        except URLError as exc:
            report_publisher_backpressure(exc)
            if isinstance(exc, HTTPError):
                exc.close()
    if response is None:
        request = Request(windows_iso_url(version), headers={"User-Agent": f"Relf/{__version__}"})
        check_canceled()
        try:
            response = build_opener(MicrosoftRedirect()).open(request, timeout=30)
        except HTTPError as exc:
            report_publisher_backpressure(exc)
            exc.close()
            raise ValueError(message("Microsoft ISO download returned HTTP {status}.", status=exc.code)) from None
        except URLError as exc:
            report_publisher_backpressure(exc)
            raise ValueError("Could not connect to the Microsoft ISO download server.") from None
    with response:
        check_canceled()
        yield response
