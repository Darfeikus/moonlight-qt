#!/usr/bin/env python3

import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "Darfeikus/moonlight-qt"
LATEST_RELEASE = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MINIMUM_RELEASE = "fork-2026.10.20"
VERSION_PATH = Path(__file__).with_name("release-version")
MAX_IMAGE_SIZE = 512 * 1024 * 1024
MAX_METADATA_SIZE = 1024 * 1024


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        if (destination.scheme != "https" or destination.netloc not in {
                "api.github.com", "github.com", "release-assets.githubusercontent.com",
                "objects.githubusercontent.com"}):
            raise ValueError("Update redirected outside GitHub HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, timeout):
    metadata = url.startswith(f"https://api.github.com/repos/{REPOSITORY}/releases/")
    request = urllib.request.Request(url, headers={
        "User-Agent": "Moonlight-fork-AppImage-updater",
        "Accept": "application/vnd.github+json" if metadata else "application/octet-stream",
        **({"Cache-Control": "no-cache"} if metadata else {}),
    })
    return urllib.request.build_opener(HttpsRedirects()).open(request, timeout=timeout)


def release_version(tag):
    if not isinstance(tag, str):
        raise ValueError("Release has no version tag")
    match = re.fullmatch(r"fork-(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?", tag)
    if not match:
        raise ValueError(f"Unsupported release tag: {tag}")
    return tuple(int(part or 0) for part in match.groups())


def release_asset(release):
    if (not isinstance(release, dict) or release.get("draft") is not False
            or release.get("prerelease") is not False):
        raise ValueError("Update metadata is not a stable release")
    if platform.machine() != "x86_64":
        raise ValueError("Unsupported AppImage architecture")
    tag = release.get("tag_name")
    if release_version(tag) < release_version(MINIMUM_RELEASE):
        raise ValueError("Refusing a release older than the patched-SDL update baseline")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Release has no asset list")
    matches = [
        asset for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
        and re.fullmatch(r"Moonlight-[A-Za-z0-9._-]+-x86_64\.AppImage", asset["name"])
    ]
    if len(matches) != 1:
        raise ValueError("Release must contain exactly one x86_64 Moonlight AppImage")
    asset = matches[0]
    if (not isinstance(asset.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"])):
        raise ValueError("Release asset has no valid SHA-256 digest")
    if type(asset.get("size")) is not int or not 64 <= asset["size"] <= MAX_IMAGE_SIZE:
        raise ValueError("Release asset has an invalid size")
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        raise ValueError("Release asset has no download URL")
    parsed = urllib.parse.urlsplit(url)
    expected_path = f"/{REPOSITORY}/releases/download/{tag}/{asset['name']}"
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.path != expected_path or parsed.query or parsed.fragment):
        raise ValueError("Release asset URL does not match its repository, tag and filename")
    return asset


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as image:
        for block in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_header(path):
    with path.open("rb") as image:
        header = image.read(64)
    if (len(header) != 64 or header[:6] != b"\x7fELF\x02\x01"
            or header[8:11] != b"AI\x02" or header[18:20] != b"\x3e\x00"):
        raise ValueError("Target is not an x86_64 type-2 AppImage; refusing to replace a native binary or launcher")


def probe_image(path):
    env = os.environ.copy()
    for name in ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "MOONLIGHT_APPIMAGE_UPDATED",
                 "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH"):
        env.pop(name, None)
    env["MOONLIGHT_DISABLE_UPDATES"] = "1"
    result = subprocess.run(
        [str(path), "--version"], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=15,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[-2048:].strip()
        raise ValueError(f"Downloaded AppImage failed its startup probe ({result.returncode}): {detail}")


def read_metadata(url):
    deadline = time.monotonic() + 5
    blocks = []
    size = 0
    with open_url(url, 3) as response:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Release metadata exceeded 5 seconds")
            block = response.read1(64 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_METADATA_SIZE:
                raise ValueError("Release metadata is too large")
            blocks.append(block)
    return json.loads(b"".join(blocks))


def update(image, release_tag=None):
    image = Path(image).resolve(strict=True)
    current = image.stat()
    if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid():
        raise ValueError("AppImage must be a regular file owned by the current user")
    verify_header(image)
    url = (LATEST_RELEASE if release_tag is None else
           f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{urllib.parse.quote(release_tag, safe='')}")
    release = read_metadata(url)
    asset = release_asset(release)
    tag = release["tag_name"]
    if release_tag is not None and tag != release_tag:
        raise ValueError("Release metadata does not match the requested tag")
    expected_digest = asset["digest"].removeprefix("sha256:")
    if file_digest(image) == expected_digest:
        return False
    if VERSION_PATH.exists():
        installed_tag = VERSION_PATH.read_text(encoding="ascii").strip()
        if release_version(tag) <= release_version(installed_tag):
            raise ValueError("Refusing stale or republished release metadata")

    download = None
    backup = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{image.name}.download-", dir=image.parent, delete=False) as output:
            download = Path(output.name)
            digest = hashlib.sha256()
            size = 0
            deadline = time.monotonic() + 30
            with open_url(asset["browser_download_url"], 5) as response:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("AppImage download exceeded 30 seconds")
                    block = response.read1(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > asset["size"]:
                        raise ValueError("AppImage download exceeds the release asset size")
                    output.write(block)
                    digest.update(block)
            if size != asset["size"] or digest.hexdigest() != expected_digest:
                raise ValueError("AppImage download failed size or SHA-256 verification")
            output.flush()
            os.fsync(output.fileno())
        verify_header(download)
        download.chmod(stat.S_IMODE(current.st_mode) | stat.S_IXUSR)
        probe_image(download)
        with tempfile.NamedTemporaryFile(prefix=f".{image.name}.backup-", dir=image.parent, delete=False) as output:
            backup = Path(output.name)
        backup.unlink()
        os.link(image, backup)
        os.replace(backup, image.with_name(image.name + ".previous"))
        os.replace(download, image)
        directory = os.open(image.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        print(f"Moonlight updater: installed {tag}; previous build saved as {image.name}.previous",
              file=sys.stderr)
        return True
    finally:
        if download is not None:
            download.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Update a Moonlight AppImage from the fork's stable releases")
    parser.add_argument("--restart-code", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release", help="Bootstrap this exact stable release tag instead of latest")
    parser.add_argument("appimage")
    args = parser.parse_args()
    try:
        if args.restart_code:
            changed = update(args.appimage, args.release)
        else:
            image = Path(args.appimage).resolve(strict=True)
            with image.with_name(image.name + ".update.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ValueError("Moonlight is running or another update is in progress") from error
                changed = update(image, args.release)
        return 10 if changed and args.restart_code else 0
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException,
            subprocess.TimeoutExpired) as error:
        print(f"Moonlight updater: {error}; keeping the installed build", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
