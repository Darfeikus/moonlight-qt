import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SteamDeckLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.launcher = self.root / "Moonlight.AppImage"
        shutil.copyfile(
            ROOT / "app/deploy/linux/steamdeck-native-launcher", self.launcher,
        )
        self.launcher.chmod(0o755)
        self.runtime = self.root / "moonlight-libs/lib"
        self.runtime.mkdir(parents=True)
        for library in ("libSDL2-2.0.so.0", "libSDL3.so.0"):
            (self.runtime / library).touch()
        payload = self.root / "moonlight-native"
        payload.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$QT_QPA_PLATFORM" "${WAYLAND_DISPLAY:-unset}" '
            '"${DISABLE_GAMESCOPE_WSI:-unset}" "$LD_LIBRARY_PATH" "$@"\n'
        )
        payload.chmod(0o755)
        self.env = dict(os.environ, QT_QPA_PLATFORM="xcb")
        for key in ("WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY",
                    "DISABLE_GAMESCOPE_WSI", "LD_LIBRARY_PATH"):
            self.env.pop(key, None)

    def run_launcher(self, launcher=None, **env):
        return subprocess.run(
            [str(launcher or self.launcher), "host name", "--option"],
            env=dict(self.env, **env), capture_output=True, text=True, timeout=5,
        )

    def test_desktop_preserves_socket_and_layer_configuration(self):
        result = self.run_launcher(
            XDG_CURRENT_DESKTOP="KDE", WAYLAND_DISPLAY="wayland-0",
            DISABLE_GAMESCOPE_WSI="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "wayland", "wayland-0", "0", str(self.runtime), "host name", "--option",
        ])

    def test_gaming_mode_uses_xcb_and_preserves_layer_configuration(self):
        result = self.run_launcher(
            XDG_CURRENT_DESKTOP="gamescope", WAYLAND_DISPLAY="stale-desktop",
            GAMESCOPE_WAYLAND_DISPLAY="gamescope-0", DISABLE_GAMESCOPE_WSI="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "xcb", "stale-desktop", "0", str(self.runtime), "host name", "--option",
        ])

    def test_gaming_mode_needs_no_native_wayland_socket(self):
        result = self.run_launcher(XDG_CURRENT_DESKTOP="gamescope")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "xcb", "unset", "unset", str(self.runtime), "host name", "--option",
        ])

    def test_private_runtime_precedes_existing_library_path(self):
        result = self.run_launcher(
            XDG_CURRENT_DESKTOP="gamescope", LD_LIBRARY_PATH="/existing/lib",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines()[3], f"{self.runtime}:/existing/lib",
        )

    def test_missing_patched_runtime_is_reported(self):
        for library in ("libSDL2-2.0.so.0", "libSDL3.so.0"):
            with self.subTest(library=library):
                path = self.runtime / library
                path.unlink()
                result = self.run_launcher(XDG_CURRENT_DESKTOP="gamescope")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"missing patched SDL runtime: {path}", result.stderr)
                self.assertEqual(result.stdout, "")
                path.touch()

    def test_symlink_resolves_binary_next_to_real_launcher(self):
        alias = self.root / "shortcut"
        alias.symlink_to(self.launcher)
        result = self.run_launcher(
            alias, XDG_CURRENT_DESKTOP="KDE", WAYLAND_DISPLAY="wayland-1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "wayland", "wayland-1", "unset", str(self.runtime), "host name", "--option",
        ])

    def test_missing_binary_is_reported(self):
        (self.root / "moonlight-native").unlink()
        result = self.run_launcher(
            XDG_CURRENT_DESKTOP="KDE", WAYLAND_DISPLAY="wayland-0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moonlight-native", result.stderr)


if __name__ == "__main__":
    unittest.main()
