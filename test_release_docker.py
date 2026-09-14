import tempfile
import unittest
from pathlib import Path

from release_docker import docker_command
from release_lib import load_config, validate_config

DOCKER_CONFIG = """\
[project]
id = "example"
version_file = "VERSION"

[docker]
image = "docker.io/example/example"
dockerfile = "Dockerfile.runtime"
platforms = ["linux/amd64", "linux/arm64"]
publish_latest = true
target = "runtime"

[docker.args]
EXAMPLE = "value"
"""


class DockerReleaseTests(unittest.TestCase):
    def test_builds_version_and_latest_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "Dockerfile.runtime").write_text("FROM scratch\n")
            (root / "release.toml").write_text(DOCKER_CONFIG)
            config = load_config(root / "release.toml")

            validate_config(config, "docker")
            command = docker_command(config, "1.2.3", "deadbeef")

            self.assertEqual(
                command[:5],
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64,linux/arm64",
                ],
            )
            self.assertIn("--build-arg", command)
            self.assertIn("EXAMPLE=value", command)
            self.assertIn("VERSION=1.2.3", command)
            self.assertIn("GIT_SHA=deadbeef", command)
            self.assertIn("--target", command)
            self.assertIn("runtime", command)
            self.assertIn("docker.io/example/example:1.2.3", command)
            self.assertIn("docker.io/example/example:latest", command)
            self.assertIn("--push", command)

    def test_omits_latest_tag_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3\n")
            (root / "Dockerfile.runtime").write_text("FROM scratch\n")
            config_text = DOCKER_CONFIG.replace(
                "publish_latest = true", "publish_latest = false"
            )
            (root / "release.toml").write_text(config_text)
            config = load_config(root / "release.toml")

            command = docker_command(config, "1.2.3", "deadbeef")

            self.assertIn("docker.io/example/example:1.2.3", command)
            self.assertNotIn("docker.io/example/example:latest", command)


if __name__ == "__main__":
    unittest.main()
