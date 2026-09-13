import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_all import copy_release_artifacts, write_checksums
from release_direct import upload_artifacts
from release_lib import load_config, release_artifacts, validate_config


def config_text(artifacts):
    return (
        "[project]\n"
        'id = "example"\n'
        'version_file = "VERSION"\n'
        "[build]\n"
        'output_dir = "dist"\n'
        "[release]\n"
        + "\n".join(
            "[[release.artifacts]]\n"
            f'name = "{name}"\n' + (f'source = "{source}"\n' if source else "")
            for name, source in artifacts
        )
        + "[hosting]\n"
        'release_prefix = "releases"\n'
    )


class DirectArtifactTests(unittest.TestCase):
    def test_build_output_copies_external_artifacts_and_checksums_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "artifacts").mkdir()
            (root / "artifacts" / "macos.dmg").write_bytes(b"dmg")
            (root / "release.toml").write_text(
                config_text([("example-{version}.dmg", "artifacts/macos.dmg")])
            )
            config = load_config(root / "release.toml")
            output = root / "output"
            output.mkdir()

            copy_release_artifacts(output, config, "1.2.3")
            write_checksums(output, ["example-1.2.3.dmg"])

            self.assertEqual((output / "example-1.2.3.dmg").read_bytes(), b"dmg")
            self.assertTrue((output / "example-1.2.3.dmg.sha256").is_file())
            self.assertIn("example-1.2.3.dmg", (output / "SHA256SUMS").read_text())

    def test_external_and_generated_artifacts_render_versioned_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "artifacts").mkdir()
            (root / "artifacts" / "windows.zip").write_bytes(b"windows")
            (root / "release.toml").write_text(
                config_text(
                    [
                        ("example-{version}-windows.zip", "artifacts/windows.zip"),
                        ("example-{version}-linux.tar.gz", None),
                    ]
                )
            )

            config = load_config(root / "release.toml")
            validate_config(config, "direct")
            artifacts = release_artifacts(config, "1.2.3")

            self.assertEqual(
                [artifact["name"] for artifact in artifacts],
                ["example-1.2.3-windows.zip", "example-1.2.3-linux.tar.gz"],
            )
            self.assertEqual(artifacts[0]["source_path"].name, "windows.zip")
            self.assertNotIn("source_path", artifacts[1])

    def test_upload_skips_existing_artifacts_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "dist").mkdir()
            artifact_path = root / "dist" / "example-1.2.3.zip"
            artifact_path.write_bytes(b"archive")
            (root / "release.toml").write_text(
                config_text([("example-{version}.zip", None)])
            )
            config = load_config(root / "release.toml")
            artifacts = release_artifacts(config, "1.2.3")
            items = [(artifacts[0], artifact_path)]

            with (
                patch("release_direct.list_prefix", return_value=set()),
                patch("release_direct.upload_file") as upload_file,
                patch("release_direct.upload_text") as upload_text,
            ):
                upload_artifacts(config, items)

            upload_file.assert_called_once_with(
                artifact_path, "releases/example-1.2.3.zip"
            )
            upload_text.assert_called_once()

            with (
                patch(
                    "release_direct.list_prefix",
                    return_value={
                        "releases/example-1.2.3.zip",
                        "releases/example-1.2.3.zip.sha256",
                    },
                ),
                patch("release_direct.upload_file") as upload_file,
                patch("release_direct.upload_text") as upload_text,
            ):
                upload_artifacts(config, items)

            upload_file.assert_not_called()
            upload_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
