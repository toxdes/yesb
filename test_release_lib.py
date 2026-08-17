import tempfile
import unittest
from pathlib import Path

from release_apt import merge_packages

from release_lib import load_config, project_version


class ProjectConfigTests(unittest.TestCase):
    def test_loads_manifest_relative_to_manifest_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "release.toml").write_text(
                '[project]\n'
                'id = "example"\n'
                'version_file = "VERSION"\n'
            )

            config = load_config(root / "release.toml")

            self.assertEqual(config.root, root)
            self.assertEqual(config.project["id"], "example")
            self.assertEqual(project_version(config), "1.2.3")


class AptMergeTests(unittest.TestCase):
    def test_preserves_existing_packages_and_replaces_same_identity(self):
        existing = (
            "Package: promptr\n"
            "Version: 1.0\n"
            "Architecture: amd64\n"
            "Filename: pool/promptr_1.0_amd64.deb\n\n"
            "Package: mousr\n"
            "Version: 1.0\n"
            "Architecture: amd64\n"
            "Filename: pool/mousr_1.0_amd64.deb\n"
        )
        current = (
            "Package: mousr\n"
            "Version: 1.1\n"
            "Architecture: amd64\n"
            "Filename: pool/mousr_1.1_amd64.deb\n"
        )

        merged = merge_packages(existing, current)

        self.assertIn("Package: promptr", merged)
        self.assertIn("Version: 1.1", merged)
        self.assertNotIn("Version: 1.0\nArchitecture: amd64\nFilename: pool/mousr_1.0", merged)


if __name__ == "__main__":
    unittest.main()
