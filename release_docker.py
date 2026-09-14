#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///

"""Build and publish an optional multi-platform Docker image.

Usage:
    ./release_docker.py

The project supplies the runtime Dockerfile. Docker must already be logged in
to the configured registry through its normal credential helper.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from release_lib import (
    DOCKER_TAG,
    check_builder_platforms,
    check_tools,
    load_config,
    load_env_file,
    project_path,
    project_version,
    run,
    validate_config,
)


def git_revision(context):
    """Return the short source revision passed to the image build."""
    result = subprocess.run(
        ["git", "-C", str(context), "rev-parse", "--short=8", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "unknown"


def docker_command(config, version, revision):
    """Build the Buildx command for the configured project image."""
    docker = config.section("docker")
    context = project_path(
        config,
        docker.get("context", "."),
        "[docker].context",
        allow_root=True,
        kind="directory",
    )
    dockerfile = project_path(
        config,
        docker.get("dockerfile", "Dockerfile.runtime"),
        "[docker].dockerfile",
        kind="file",
    )
    platforms = docker.get("platforms", ["linux/amd64"])
    build_args = dict(docker.get("args", {}))
    build_args.setdefault("VERSION", version)
    build_args.setdefault("GIT_SHA", revision)

    image = docker["image"]
    tags = [f"{image}:{version}"]
    if docker.get("publish_latest", False):
        tags.append(f"{image}:latest")

    command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        ",".join(platforms),
        "--pull",
    ]
    for key, value in build_args.items():
        command.extend(["--build-arg", f"{key}={value}"])
    if docker.get("target"):
        command.extend(["--target", docker["target"]])
    for tag in tags:
        command.extend(["--tag", tag])
    command.extend(["--push", "--progress=plain", "-f", str(dockerfile), str(context)])
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", metavar="PATH", help="Load env vars from file (KEY=VALUE per line)"
    )
    parser.add_argument(
        "--project-root", default=".", help="Project directory containing release.toml"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Docker command without running it",
    )
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    config = load_config(Path(args.project_root) / "release.toml")
    if not config.section("docker"):
        print("No [docker] configured; skipping Docker release")
        return 0
    validate_config(config, "docker")
    check_tools("docker")

    version = project_version(config)
    if not DOCKER_TAG.fullmatch(version):
        print(
            "Error: project version is not a valid Docker tag: " + version,
            file=sys.stderr,
        )
        return 1

    docker = config.section("docker")
    platforms = docker.get("platforms", ["linux/amd64"])
    context = project_path(
        config,
        docker.get("context", "."),
        "[docker].context",
        allow_root=True,
        kind="directory",
    )
    command = docker_command(config, version, git_revision(context))

    if args.dry_run:
        print("  $ " + " ".join(command))
        return 0

    try:
        run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
        )
        run(["docker", "buildx", "version"], capture_output=True)
        check_builder_platforms(platforms)
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Error: Docker Buildx is not ready: {error}", file=sys.stderr)
        return 1

    print(f"\nPublishing Docker image for {', '.join(platforms)} ...\n")
    run(command)
    print("Docker image published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
