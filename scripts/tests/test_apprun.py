import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AppRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.appdir = self.root / "AppDir"
        (self.appdir / "usr/bin").mkdir(parents=True)
        program = self.appdir / "usr/bin/moonlight"
        program.write_text(
            '#!/bin/bash\nprintf "arg=%s\\n" "$@"\n'
            'echo "libva=${VAAPI_USE_FALLBACK_PATHS:-0}"\n'
            'echo "library_path=${LD_LIBRARY_PATH:-}"\n'
            'echo "qpa=${QT_QPA_PLATFORM:-}"\n'
            'echo "vulkan=${PREFER_VULKAN:-}"\n'
            'echo "driver=${LIBVA_DRIVER_NAME:-}"\n'
        )
        program.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        for name in ("python3", "flock", "curl", "wget"):
            command = self.bin / name
            command.write_text(
                '#!/bin/bash\necho called >> "$CALLS"\nexit 99\n'
            )
            command.chmod(0o755)
        self.image = self.root / "Moonlight.AppImage"
        self.image.write_bytes(b"installed build")
        self.env = dict(
            os.environ, APPDIR=str(self.appdir), APPIMAGE=str(self.image),
            PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls),
        )
        for key in ("MOONLIGHT_DISABLE_UPDATES", "MOONLIGHT_APPIMAGE_UPDATED",
                    "VAAPI_USE_FALLBACK_PATHS", "LD_LIBRARY_PATH"):
            self.env.pop(key, None)

    def run_app(self, args=(), **env):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "app/deploy/linux/AppRun"), *args],
            env=dict(self.env, **env), capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.image.read_bytes(), b"installed build")
        self.assertFalse(self.image.with_name(self.image.name + ".update.lock").exists())
        self.assertFalse(self.image.with_name(self.image.name + ".previous").exists())
        return result

    def test_normal_launch_never_updates_and_preserves_arguments(self):
        result = self.run_app(["stream", "host name", "", "--option"])
        self.assertEqual(
            result.stdout.splitlines()[:4],
            ["arg=stream", "arg=host name", "arg=", "arg=--option"],
        )

    def test_help_and_version_never_update(self):
        for arg in ("--help", "-h", "--version", "-v"):
            with self.subTest(arg=arg):
                self.assertIn(f"arg={arg}", self.run_app([arg]).stdout)

    def test_extracted_appdir_launches_without_appimage(self):
        del self.env["APPIMAGE"]
        self.run_app()

    def test_graphics_overrides_are_preserved(self):
        result = self.run_app(
            QT_QPA_PLATFORM="wayland", PREFER_VULKAN="1",
            LIBVA_DRIVER_NAME="radeonsi", LD_LIBRARY_PATH="/host/lib",
        )
        for value in ("qpa=wayland", "vulkan=1", "driver=radeonsi",
                      "library_path=/host/lib", "libva=0"):
            self.assertIn(value, result.stdout.splitlines())

    def test_libva_fallback_is_used_only_when_probe_fails(self):
        fallback = self.appdir / "opt/libva-fallback"
        fallback.mkdir(parents=True)
        probe = fallback / "libva-probe"
        for status in (0, 1):
            with self.subTest(status=status):
                probe.write_text(f"#!/bin/sh\nexit {status}\n")
                probe.chmod(0o755)
                result = self.run_app(LD_LIBRARY_PATH="/host/lib")
                self.assertIn(f"libva={status}", result.stdout.splitlines())
                expected = f"{fallback}:/host/lib" if status else "/host/lib"
                self.assertIn(f"library_path={expected}", result.stdout.splitlines())

    def test_libva_fallback_without_existing_library_path(self):
        fallback = self.appdir / "opt/libva-fallback"
        fallback.mkdir(parents=True)
        probe = fallback / "libva-probe"
        probe.write_text("#!/bin/sh\nexit 1\n")
        probe.chmod(0o755)
        result = self.run_app()
        self.assertIn(f"library_path={fallback}", result.stdout.splitlines())

    def test_launch_does_not_require_writable_image_directory(self):
        self.image.with_name(self.image.name + ".update.lock").mkdir()
        self.image.parent.chmod(0o555)
        try:
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "app/deploy/linux/AppRun")],
                env=self.env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertFalse(self.calls.exists())
        finally:
            self.image.parent.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
