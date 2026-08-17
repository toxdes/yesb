import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
