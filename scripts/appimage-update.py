#!/usr/bin/env python3

import argparse
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "Darfeikus/moonlight-qt"
LATEST_RELEASE = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_IMAGE_SIZE = 512 * 1024 * 1024
BUILD_TIME_PATH = Path(__file__).with_name("build-time")


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        if destination.scheme != "https" or destination.hostname not in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }:
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


def release_asset(release):
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("Update metadata is not a stable release")
    arch = platform.machine()
    if arch != "x86_64":
        raise ValueError(f"Unsupported AppImage architecture: {arch}")
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
    digest = asset.get("digest")
    size = asset.get("size")
    url = asset.get("browser_download_url")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Release asset has no valid SHA-256 digest")
    if type(size) is not int or not 64 <= size <= MAX_IMAGE_SIZE:
        raise ValueError("Release asset has an invalid size")
    if not isinstance(url, str):
        raise ValueError("Release asset has no download URL")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
            or parsed.path.rsplit("/", 1)[-1] != asset["name"]
            or parsed.query or parsed.fragment):
        raise ValueError("Release asset URL is outside the configured repository")
    return asset


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as image:
        for block in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_release_age(release):
    if not BUILD_TIME_PATH.exists():
        return
    built_at = int(BUILD_TIME_PATH.read_text(encoding="ascii").strip())
    published = release.get("published_at")
    if built_at <= 0 or not isinstance(published, str):
        raise ValueError("Missing or invalid release/build timestamp")
    published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if published_at.tzinfo is None:
        raise ValueError("Release timestamp has no timezone")
    if published_at.timestamp() < built_at:
        raise ValueError("Refusing stale release metadata older than this AppImage build")


def update(image, release_tag=None):
    image = Path(image).resolve(strict=True)
    current = image.stat()
    if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid():
        raise ValueError("AppImage must be a regular file owned by the current user")
    with image.open("rb") as installed:
        if installed.read(4) != b"\x7fELF":
            raise ValueError("Installed target is not an ELF executable; refusing to replace a launcher script")
    url = (LATEST_RELEASE if release_tag is None else
           f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{urllib.parse.quote(release_tag, safe='')}")
    with open_url(url, 3) as response:
        metadata = response.read(1024 * 1024 + 1)
    if len(metadata) > 1024 * 1024:
        raise ValueError("Release metadata is too large")
    release = json.loads(metadata)
    asset = release_asset(release)
    if release_tag is not None and release.get("tag_name") != release_tag:
        raise ValueError("Release metadata does not match the requested tag")
    expected_digest = asset["digest"].removeprefix("sha256:")
    if file_digest(image) == expected_digest:
        return False
    check_release_age(release)

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
        with download.open("rb") as candidate:
            header = candidate.read(64)
        if (header[:6] != b"\x7fELF\x02\x01" or header[8:11] != b"AI\x02"
                or header[18:20] != b"\x3e\x00"):
            raise ValueError("Download is not an x86_64 type-2 AppImage")
        download.chmod(stat.S_IMODE(current.st_mode) | stat.S_IXUSR)
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
        print(f"Moonlight updater: installed {asset['name']}; previous build saved as {image.name}.previous",
              file=sys.stderr)
        return True
    finally:
        if download is not None:
            download.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Update an existing Moonlight executable from the fork's latest stable release")
    parser.add_argument("--restart-code", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release", help="Install this exact stable release tag instead of latest")
    parser.add_argument("appimage")
    args = parser.parse_args()
    try:
        changed = update(args.appimage, args.release)
        return 10 if changed and args.restart_code else 0
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
        print(f"Moonlight updater: {error}; starting the installed build", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
