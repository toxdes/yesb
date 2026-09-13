import tempfile
import unittest
from pathlib import Path

from release_homebrew import find_archives, generate_formula, tap_remote
from release_lib import load_config, validate_config

HOMEbrew_CONFIG = """\
[project]
id = "example"
name = "Example"
description = "An example command"
homepage = "https://example.com"
version_file = "VERSION"

[build]
output_dir = "dist"

[release]
[[release.artifacts]]
name = "example-{version}-macos-x86_64.zip"

[[release.artifacts]]
name = "example-{version}-macos-arm64.zip"

[homebrew]
tap = "example/tap"
formula = "example"
binary = "example"

[[homebrew.assets]]
source = "share/man/man1/example.1"
destination = "man1"

[[homebrew.archives]]
artifact = "example-{version}-macos-x86_64.zip"
architecture = "x86_64"
url = "https://downloads.example.com/example-{version}-macos-x86_64.zip"

[[homebrew.archives]]
artifact = "example-{version}-macos-arm64.zip"
architecture = "arm64"
url = "https://downloads.example.com/example-{version}-macos-arm64.zip"
"""


class HomebrewFormulaTests(unittest.TestCase):
    def test_generates_architecture_specific_formula_without_uploading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            dist = root / "dist"
            dist.mkdir()
            (dist / "example-1.2.3-macos-x86_64.zip").write_bytes(b"intel")
            (dist / "example-1.2.3-macos-arm64.zip").write_bytes(b"arm")
            (root / "release.toml").write_text(HOMEbrew_CONFIG)
            config = load_config(root / "release.toml")

            validate_config(config, "homebrew")
            archives = find_archives(config, "1.2.3")
            formula = generate_formula(config, "1.2.3", archives)

            self.assertIn("class Example < Formula", formula)
            self.assertIn("on_intel do", formula)
            self.assertIn("on_arm do", formula)
            self.assertIn('bin.install "example"', formula)
            self.assertIn(
                'man1.install "share/man/man1/example.1"', formula
            )
            self.assertIn(
                "https://downloads.example.com/example-1.2.3-macos-x86_64.zip", formula
            )
            self.assertIn(
                "https://downloads.example.com/example-1.2.3-macos-arm64.zip", formula
            )

    def test_derives_github_tap_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.0.0\n")
            (root / "release.toml").write_text(HOMEbrew_CONFIG)
            config = load_config(root / "release.toml")

            self.assertEqual(
                tap_remote(config.section("homebrew")),
                "git@github.com:example/homebrew-tap.git",
            )


if __name__ == "__main__":
    unittest.main()
