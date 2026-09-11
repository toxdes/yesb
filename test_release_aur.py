import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from release_aur import generate_srcinfo
from release_lib import load_config, validate_config


class SrcinfoHelperTests(unittest.TestCase):
    def test_helper_runs_isolated_and_returns_srcinfo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PKGBUILD").write_text("pkgname = example\n")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="pkgbase = example\n", stderr=""
            )

            with patch("release_aur.subprocess.run", return_value=completed) as run:
                srcinfo = generate_srcinfo(
                    repo,
                    "registry.example.com/yesb-makepkg@sha256:" + "a" * 64,
                )

            self.assertEqual(srcinfo, "pkgbase = example\n")
            command = run.call_args.args[0]
            self.assertIn("--network=none", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertNotIn("/root/.ssh", " ".join(command))
            self.assertIn(
                "type=bind,src=" + str(repo) + ",dst=/input,readonly", command
            )
            self.assertIn(
                "/work:rw,noexec,nosuid,size=64m,uid=", " ".join(command)
            )
            self.assertEqual(
                command[-3:],
                [
                    "sh",
                    "-c",
                    "cp -- /input/PKGBUILD /work/PKGBUILD && makepkg --printsrcinfo",
                ],
            )

    def test_helper_image_must_be_digest_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.0.0\n")
            (root / "release.toml").write_text(
                "[project]\n"
                'id = "example"\n'
                'version_file = "VERSION"\n'
                "[aur]\n"
                'srcinfo_helper_image = "archlinux:latest"\n'
            )

            config = load_config(root / "release.toml")
            with self.assertRaises(SystemExit):
                validate_config(config, "aur")

            config.data["aur"]["srcinfo_helper_image"] = (
                "registry.example.com/yesb-makepkg@sha256:" + "a" * 64
            )
            validate_config(config, "aur")


if __name__ == "__main__":
    unittest.main()
