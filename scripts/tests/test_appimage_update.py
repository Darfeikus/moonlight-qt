import hashlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("appimage_update", ROOT / "scripts/appimage-update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def image_bytes(payload):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[8:11] = b"AI\x02"
    header[18:20] = b"\x3e\x00"
    return bytes(header) + payload


def release(data, tag="fork-2026.10.20"):
    name = "Moonlight-fork-x86_64.AppImage"
    return dict(draft=False, prerelease=False, tag_name=tag, assets=[{
        "name": name, "size": len(data),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "browser_download_url": f"https://github.com/{updater.REPOSITORY}/releases/download/{tag}/{name}",
    }])


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.image = self.root / "Moonlight.AppImage"
        self.old = image_bytes(b"old")
        self.new = image_bytes(b"new")
        self.image.write_bytes(self.old)
        self.image.chmod(0o751)
        self.version = self.root / "release-version"
        for name, value in (("VERSION_PATH", self.version),):
            mock = patch.object(updater, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.arch = patch.object(updater.platform, "machine", return_value="x86_64")
        self.arch.start()
        self.addCleanup(self.arch.stop)
        self.probe = patch.object(updater, "probe_image")
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)

    def apply(self, data=None, metadata=None, tag=None):
        data = self.new if data is None else data
        metadata = release(data) if metadata is None else metadata
        with patch.object(updater, "open_url", side_effect=[
            io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(data),
        ]) as request:
            changed = updater.update(self.image, tag)
        return changed, request

    def assert_unchanged(self):
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertFalse(self.image.with_name(self.image.name + ".previous").exists())
        self.assertFalse(list(self.root.glob(".*.download-*")))
        self.assertFalse(list(self.root.glob(".*.backup-*")))

    def test_verified_update_preserves_backup_and_permissions(self):
        changed, _ = self.apply()
        self.assertTrue(changed)
        self.assertEqual(self.image.read_bytes(), self.new)
        self.assertEqual(self.image.with_name(self.image.name + ".previous").read_bytes(), self.old)
        self.assertEqual(stat.S_IMODE(self.image.stat().st_mode), 0o751)
        self.probe_mock.assert_called_once()

    def test_equal_digest_never_downloads_or_probes(self):
        changed, request = self.apply(self.old)
        self.assertFalse(changed)
        self.assertEqual(request.call_count, 1)
        self.probe_mock.assert_not_called()
        self.assert_unchanged()

    def test_truncated_oversized_and_corrupt_downloads_keep_installed_image(self):
        for data in (self.new[:-1], self.new + b"x", image_bytes(b"bad")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.apply(data, release(self.new))
            self.assert_unchanged()

    def test_invalid_appimage_or_architecture_is_rejected(self):
        for offset, value in ((8, b"bad"), (18, b"\xb7\x00")):
            data = bytearray(self.new)
            data[offset:offset + len(value)] = value
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "type-2 AppImage"):
                self.apply(bytes(data))
            self.assert_unchanged()

    def test_native_executables_and_wrappers_are_not_update_targets(self):
        for data in (b"#!/bin/sh\nexit 0\n", b"\x7fELF" + bytes(100)):
            self.image.write_bytes(data)
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "native binary or launcher"):
                self.apply()
            self.assertEqual(self.image.read_bytes(), data)

    def test_asset_validation(self):
        for field, value in (
            ("digest", None), ("digest", "sha256:bad"), ("size", True),
            ("size", updater.MAX_IMAGE_SIZE + 1), ("browser_download_url", None),
            ("browser_download_url", "http://github.com/file"),
            ("browser_download_url", "https://github.com/other/repo/file"),
            ("browser_download_url", "https://evil.example/file"),
            ("browser_download_url", release(self.new, "fork-2026.10.21")["assets"][0]["browser_download_url"]),
        ):
            metadata = release(self.new)
            metadata["assets"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                updater.release_asset(metadata)

    def test_only_one_stable_asset_is_accepted(self):
        for metadata in (None, [], {}, dict(release(self.new), draft=True),
                         dict(release(self.new), prerelease=True),
                         dict(release(self.new), assets=[]),
                         dict(release(self.new), assets=release(self.new)["assets"] * 2)):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                updater.release_asset(metadata)

    def test_pre_sdl_fix_release_cannot_be_installed(self):
        with self.assertRaisesRegex(ValueError, "patched-SDL"):
            self.apply(metadata=release(self.new, "fork-2026.10.19.6"))
        self.assert_unchanged()

    def test_stale_or_republished_tag_cannot_replace_current_build(self):
        self.version.write_text("fork-2026.10.21")
        for tag in ("fork-2026.10.20", "fork-2026.10.21"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "stale or republished"):
                self.apply(metadata=release(self.new, tag))
            self.assert_unchanged()

    def test_numeric_release_revision_order(self):
        self.version.write_text("fork-2026.10.20.9")
        self.assertTrue(self.apply(metadata=release(self.new, "fork-2026.10.20.10"))[0])

    def test_unchanged_build_accepts_matching_digest(self):
        self.version.write_text("fork-2026.10.20")
        self.assertFalse(self.apply(self.old)[0])

    def test_invalid_release_tag_is_rejected(self):
        for tag in (None, "master", "fork-2026.10.20-rc1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                updater.release_version(tag)

    def test_wrong_machine_is_rejected(self):
        with patch.object(updater.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(ValueError, "architecture"):
                self.apply()

    def test_exact_tag_checks_metadata_tag(self):
        changed, request = self.apply(tag="fork-2026.10.20")
        self.assertTrue(changed)
        self.assertTrue(request.call_args_list[0].args[0].endswith("/tags/fork-2026.10.20"))

    def test_wrong_exact_tag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requested tag"):
            self.apply(tag="fork-2026.10.21")
        self.assert_unchanged()

    def test_metadata_requests_revalidate_cache(self):
        with patch.object(updater.urllib.request, "build_opener") as opener:
            updater.open_url(updater.LATEST_RELEASE, 3)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Cache-control"), "no-cache")

    def test_bad_redirects_are_rejected(self):
        handler = updater.HttpsRedirects()
        for url in ("http://github.com/file", "https://evil.example/file",
                    "https://user@github.com/file", "https://github.com:123/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "", {}, url)

    def test_metadata_size_limit(self):
        with patch.object(updater, "open_url", return_value=io.BytesIO(b"x" * (updater.MAX_METADATA_SIZE + 1))):
            with self.assertRaisesRegex(ValueError, "too large"):
                updater.read_metadata(updater.LATEST_RELEASE)

    def test_download_deadline(self):
        with patch.object(updater, "read_metadata", return_value=release(self.new)):
            with patch.object(updater.time, "monotonic", side_effect=[0, 31]):
                with self.assertRaisesRegex(TimeoutError, "30 seconds"):
                    self.apply()
        self.assert_unchanged()

    def test_failed_probe_leaves_image_and_existing_backup_untouched(self):
        previous = self.image.with_name(self.image.name + ".previous")
        previous.write_bytes(b"known working")
        self.probe_mock.side_effect = ValueError("probe rejected")
        with self.assertRaisesRegex(ValueError, "probe rejected"):
            self.apply()
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertEqual(previous.read_bytes(), b"known working")
        self.assertFalse(list(self.root.glob(".*.download-*")))

    def test_failed_install_keeps_image_and_recoverable_backup(self):
        replace = updater.os.replace

        def fail_install(source, destination):
            if destination == self.image:
                raise OSError("replacement denied")
            return replace(source, destination)

        with patch.object(updater.os, "replace", side_effect=fail_install):
            with self.assertRaisesRegex(OSError, "replacement denied"):
                self.apply()
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertEqual(self.image.with_name(self.image.name + ".previous").read_bytes(), self.old)

    def test_symlink_and_launcher_path_are_preserved(self):
        link = self.root / "shortcut"
        link.symlink_to(self.image)
        with patch.object(updater, "open_url", side_effect=[
            io.BytesIO(json.dumps(release(self.new)).encode()), io.BytesIO(self.new),
        ]):
            self.assertTrue(updater.update(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), self.new)

    def test_offline_failure_is_explicit(self):
        with patch.object(sys, "argv", ["updater", str(self.image)]):
            with patch.object(updater, "open_url", side_effect=urllib.error.URLError("offline")):
                with patch("sys.stderr", new_callable=io.StringIO) as log:
                    self.assertEqual(updater.main(), 1)
        self.assertIn("offline", log.getvalue())
        self.assert_unchanged()

    def test_bootstrap_refuses_to_update_a_running_image(self):
        lock_path = self.image.with_name(self.image.name + ".update.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            with patch.object(sys, "argv", ["updater", str(self.image)]):
                with patch.object(updater, "update") as update:
                    with patch("sys.stderr", new_callable=io.StringIO) as log:
                        self.assertEqual(updater.main(), 1)
        self.assertIn("Moonlight is running", log.getvalue())
        update.assert_not_called()

    def test_restart_code(self):
        for flags, expected in (([], 0), (["--restart-code"], 10)):
            with patch.object(sys, "argv", ["updater", *flags, str(self.image)]):
                with patch.object(updater, "update", return_value=True):
                    self.assertEqual(updater.main(), expected)

    def test_probe_preserves_graphics_but_removes_appimage_and_loader_environment(self):
        self.probe.stop()
        env = dict(APPDIR="/old/mount", APPIMAGE="/old/image", ARGV0="old",
                   LD_LIBRARY_PATH="/bundled", LD_PRELOAD="/bundled/preload",
                   PYTHONHOME="/bundled/python", PYTHONPATH="/bundled/python",
                   VK_DRIVER_FILES="/host/nvidia.json", PREFER_VULKAN="1")
        with patch.dict(os.environ, env):
            with patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
                updater.probe_image(self.image)
        actual = run.call_args.kwargs["env"]
        for key in ("APPDIR", "APPIMAGE", "ARGV0", "LD_LIBRARY_PATH",
                    "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH"):
            self.assertNotIn(key, actual)
        self.assertEqual(actual["VK_DRIVER_FILES"], "/host/nvidia.json")
        self.assertEqual(actual["PREFER_VULKAN"], "1")
        self.assertEqual(actual["MOONLIGHT_DISABLE_UPDATES"], "1")

    def test_probe_failure_is_reported(self):
        self.probe.stop()
        with patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"missing library")):
            with self.assertRaisesRegex(ValueError, "missing library"):
                updater.probe_image(self.image)


if __name__ == "__main__":
    unittest.main()
