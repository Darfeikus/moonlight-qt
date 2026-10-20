import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
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


def release(data):
    return {
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": "Moonlight-fork-x86_64.AppImage",
            "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": (
                "https://github.com/Darfeikus/moonlight-qt/releases/download/"
                "fork-2026.10.19/Moonlight-fork-x86_64.AppImage"
            ),
        }],
    }


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / "Moonlight.AppImage"
        self.old = image_bytes(b"old")
        self.new = image_bytes(b"new")
        self.image.write_bytes(self.old)
        self.image.chmod(0o751)
        self.arch = patch.object(updater.platform, "machine", return_value="x86_64")
        self.arch.start()
        self.addCleanup(self.arch.stop)
        self.build_time = self.image.parent / "build-time"
        self.timestamp_path = patch.object(updater, "BUILD_TIME_PATH", self.build_time)
        self.timestamp_path.start()
        self.addCleanup(self.timestamp_path.stop)

    def apply(self, data=None, metadata=None):
        data = self.new if data is None else data
        metadata = release(data) if metadata is None else metadata
        with patch.object(updater, "open_url", side_effect=[
            io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(data),
        ]) as request:
            changed = updater.update(self.image)
        return changed, request

    def assert_unchanged(self):
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertEqual(list(self.image.parent.iterdir()), [self.image])

    def test_installs_verified_asset_and_preserves_previous_and_permissions(self):
        changed, _ = self.apply()
        self.assertTrue(changed)
        self.assertEqual(self.image.read_bytes(), self.new)
        self.assertEqual(self.image.with_name("Moonlight.AppImage.previous").read_bytes(), self.old)
        self.assertEqual(stat.S_IMODE(self.image.stat().st_mode), 0o751)
        self.assertEqual(len(list(self.image.parent.iterdir())), 2)

    def test_unchanged_digest_does_not_download(self):
        changed, request = self.apply(self.old)
        self.assertFalse(changed)
        self.assertEqual(request.call_count, 1)
        self.assert_unchanged()

    def test_failed_verification_leaves_installed_image_and_no_temporary_files(self):
        for data in (self.new[:-1], self.new + b"extra", image_bytes(b"bad")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.apply(data, release(self.new))
            self.assert_unchanged()

    def test_non_appimage_download_is_rejected_even_with_matching_digest(self):
        data = bytearray(self.new)
        data[8:11] = b"bad"
        with self.assertRaisesRegex(ValueError, "type-2 AppImage"):
            self.apply(bytes(data))
        self.assert_unchanged()

    def test_wrong_architecture_download_is_rejected(self):
        data = bytearray(self.new)
        data[18:20] = b"\xb7\x00"
        with self.assertRaisesRegex(ValueError, "x86_64"):
            self.apply(bytes(data))
        self.assert_unchanged()

    def test_metadata_validation(self):
        for field, value in (
            ("digest", None), ("digest", "sha256:bad"), ("size", True),
            ("size", updater.MAX_IMAGE_SIZE + 1),
            ("browser_download_url", "http://github.com/file"),
            ("browser_download_url", "https://github.com/other/repo/file"),
            ("browser_download_url", "https://evil.example/Moonlight-fork-x86_64.AppImage"),
        ):
            metadata = release(self.new)
            metadata["assets"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                updater.release_asset(metadata)

    def test_only_one_stable_matching_asset_is_accepted(self):
        for metadata in (
            None, [], {}, {"draft": True, "prerelease": False, "assets": []},
            {"draft": False, "prerelease": True, "assets": []},
            {"draft": False, "prerelease": False, "assets": []},
            {"draft": False, "prerelease": False, "assets": release(self.new)["assets"] * 2},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                updater.release_asset(metadata)

    def test_unsupported_machine_is_rejected(self):
        with patch.object(updater.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(ValueError, "architecture"):
                updater.release_asset(release(self.new))

    def test_offline_failure_is_logged_and_returns_failure(self):
        with patch.object(sys, "argv", ["updater", str(self.image)]):
            with patch.object(updater, "open_url", side_effect=urllib.error.URLError("offline")):
                with patch("sys.stderr", new_callable=io.StringIO) as log:
                    self.assertEqual(updater.main(), 1)
        self.assertIn("offline", log.getvalue())
        self.assert_unchanged()

    def test_redirects_cannot_downgrade_https_or_leave_github(self):
        handler = updater.HttpsRedirects()
        for url in ("http://github.com/file", "https://evil.example/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "", {}, url)

    def test_download_deadline_keeps_old_image(self):
        with patch.object(updater.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(TimeoutError, "30 seconds"):
                self.apply()
        self.assert_unchanged()

    def test_failed_atomic_replace_preserves_installed_image(self):
        replace = updater.os.replace

        def fail_install(source, destination):
            if destination == self.image:
                raise OSError("replacement denied")
            return replace(source, destination)

        with patch.object(updater.os, "replace", side_effect=fail_install):
            with self.assertRaisesRegex(OSError, "replacement denied"):
                self.apply()
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertEqual(self.image.with_name("Moonlight.AppImage.previous").read_bytes(), self.old)
        self.assertEqual(len(list(self.image.parent.iterdir())), 2)

    def test_symlink_is_preserved_and_target_is_updated(self):
        link = self.image.parent / "moonlight"
        link.symlink_to(self.image)
        with patch.object(updater, "open_url", side_effect=[
            io.BytesIO(json.dumps(release(self.new)).encode()), io.BytesIO(self.new),
        ]):
            self.assertTrue(updater.update(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), self.new)

    def test_launcher_scripts_are_never_replaced(self):
        self.image.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "launcher script"):
            updater.update(self.image)

    def test_bootstrap_returns_zero_but_apprun_requests_restart(self):
        for flags, expected in (([], 0), (["--restart-code"], 10)):
            with patch.object(sys, "argv", ["updater", *flags, str(self.image)]):
                with patch.object(updater, "update", return_value=True):
                    self.assertEqual(updater.main(), expected)

    def test_stale_latest_metadata_cannot_downgrade_a_newer_build(self):
        self.build_time.write_text("200")
        metadata = release(self.new)
        metadata["published_at"] = "1970-01-01T00:01:40Z"
        with self.assertRaisesRegex(ValueError, "stale release metadata"):
            self.apply(metadata=metadata)
        self.assertEqual(self.image.read_bytes(), self.old)
        self.assertFalse(self.image.with_name("Moonlight.AppImage.previous").exists())

    def test_release_published_after_build_is_accepted(self):
        self.build_time.write_text("100")
        metadata = release(self.new)
        metadata["published_at"] = "1970-01-01T00:03:20Z"
        changed, _ = self.apply(metadata=metadata)
        self.assertTrue(changed)
        self.assertEqual(self.image.read_bytes(), self.new)

    def test_unchanged_image_does_not_require_publication_timestamp(self):
        self.build_time.write_text("200")
        changed, request = self.apply(self.old)
        self.assertFalse(changed)
        self.assertEqual(request.call_count, 1)

    def test_exact_release_uses_tag_endpoint_and_checks_tag(self):
        metadata = release(self.new)
        metadata["tag_name"] = "fork-test"
        with patch.object(updater, "open_url", side_effect=[
            io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(self.new),
        ]) as request:
            self.assertTrue(updater.update(self.image, "fork-test"))
        self.assertEqual(request.call_args_list[0].args[0],
                         "https://api.github.com/repos/Darfeikus/moonlight-qt/releases/tags/fork-test")

    def test_wrong_exact_release_is_rejected(self):
        metadata = release(self.new)
        metadata["tag_name"] = "wrong-tag"
        with patch.object(updater, "open_url", return_value=io.BytesIO(json.dumps(metadata).encode())):
            with self.assertRaisesRegex(ValueError, "requested tag"):
                updater.update(self.image, "fork-test")
        self.assert_unchanged()

    def test_metadata_requests_revalidate_cache(self):
        with patch.object(updater.urllib.request, "build_opener") as opener:
            updater.open_url(updater.LATEST_RELEASE, 3)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Cache-control"), "no-cache")


class AppRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.appdir = self.root / "AppDir"
        (self.appdir / "usr/bin").mkdir(parents=True)
        self.program = self.appdir / "usr/bin/moonlight"
        self.program.write_text(
            '#!/bin/bash\nprintf "arg=%s\\n" "$@"\n'
            'echo "libva=${VAAPI_USE_FALLBACK_PATHS:-0}"\n'
            'if [ -n "${HOLD_FILE:-}" ]; then touch "$HOLD_FILE"; exec sleep 10; fi\n'
        )
        self.program.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        python = self.bin / "python3"
        python.write_text('#!/bin/bash\necho called >> "$CALLS"\nexit "${UPDATE_STATUS:-0}"\n')
        python.chmod(0o755)
        self.apprun = ROOT / "app/deploy/linux/AppRun"
        self.image = self.root / "Moonlight.AppImage"
        self.image.write_text(
            '#!/bin/bash\nexport APPDIR="$FAKE_APPDIR"\nexport APPIMAGE="$0"\n'
            'exec /bin/bash "$TEST_APPRUN" "$@"\n'
        )
        self.image.chmod(0o755)
        self.env = dict(os.environ, APPDIR=str(self.appdir), APPIMAGE=str(self.image),
                        PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls),
                        FAKE_APPDIR=str(self.appdir), TEST_APPRUN=str(self.apprun))
        for key in ("MOONLIGHT_DISABLE_UPDATES", "MOONLIGHT_APPIMAGE_UPDATED",
                    "VAAPI_USE_FALLBACK_PATHS", "HOLD_FILE"):
            self.env.pop(key, None)

    def run_app(self, args=(), **env):
        return subprocess.run(
            ["/bin/bash", str(self.apprun), *args],
            env=dict(self.env, **env), capture_output=True, text=True, timeout=5,
        )

    def test_normal_launch_checks_and_preserves_arguments(self):
        result = self.run_app(["stream", "host name", "--option"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arg=host name", result.stdout)
        self.assertEqual(self.calls.read_text(), "called\n")

    def test_success_reexecutes_once_and_keeps_arguments(self):
        result = self.run_app(["argument with spaces"], UPDATE_STATUS="10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arg=argument with spaces", result.stdout)
        self.assertEqual(self.calls.read_text(), "called\n")

    def test_offline_check_still_launches_installed_binary(self):
        result = self.run_app(UPDATE_STATUS="1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("update check failed", result.stderr)

    def test_help_version_and_opt_out_skip_checks(self):
        for arg in ("--help", "-h", "--version", "-v"):
            result = self.run_app([arg])
            self.assertEqual(result.returncode, 0)
        self.assertEqual(self.run_app(MOONLIGHT_DISABLE_UPDATES="1").returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_extracted_appdir_skips_check(self):
        env = self.env.copy()
        del env["APPIMAGE"]
        result = subprocess.run(["bash", str(self.apprun)], env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_libva_fallback_is_still_used_only_when_probe_fails(self):
        fallback = self.appdir / "opt/libva-fallback"
        fallback.mkdir(parents=True)
        probe = fallback / "libva-probe"
        for status in (0, 1):
            probe.write_text(f"#!/bin/sh\nexit {status}\n")
            probe.chmod(0o755)
            result = self.run_app(["--version"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"libva={status}", result.stdout)

    def test_unwritable_lock_logs_and_still_launches(self):
        self.image.with_name(self.image.name + ".update.lock").mkdir()
        result = self.run_app()
        self.assertEqual(result.returncode, 0)
        self.assertIn("cannot open update lock", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_running_instance_prevents_another_update_check(self):
        ready = self.root / "ready"
        first = subprocess.Popen(
            ["bash", str(self.apprun)], env=dict(self.env, HOLD_FILE=str(ready)),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            result = self.run_app()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.calls.read_text(), "called\n")
        finally:
            first.terminate()
            first.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
