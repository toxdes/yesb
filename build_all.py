#!/usr/bin/env python3
"""Build a project's release artifacts with Docker Buildx.

Usage:
    ./yesb/build_all.py [--project-root PATH] [--include-appimage]
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from release_lib import load_config, project_path, project_version, run, validate_config


def flatten_output(output_dir):
    """Move files emitted in per-platform directories to output_dir."""
    for child in sorted(output_dir.iterdir()):
        if not child.is_dir() or child.name == ".git":
            continue
        for artifact in child.iterdir():
            if artifact.is_file():
                shutil.move(str(artifact), str(output_dir / artifact.name))
        child.rmdir()


def write_checksums(output_dir):
    suffixes = (".deb", ".rpm", ".AppImage", ".tar.gz")
    sums = []

    for artifact in sorted(output_dir.iterdir()):
        if not artifact.is_file() or not artifact.name.endswith(suffixes):
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        line = f"{digest}  {artifact.name}\n"
        (output_dir / f"{artifact.name}.sha256").write_text(line)
        sums.append(line.rstrip("\n"))

    if sums:
        (output_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")


def git_revision(context):
    result = subprocess.run(
        ["git", "-C", str(context), "rev-parse", "--short=8", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project directory containing release.toml (default: current directory)",
    )
    parser.add_argument(
        "--include-appimage",
        action="store_true",
        help="Request AppImage output from the project Dockerfile",
    )
    args = parser.parse_args()

    config = load_config(Path(args.project_root) / "release.toml")
    validate_config(config, "build")
    build = config.section("build")
    output_dir = project_path(
        config, build.get("output_dir", "dist"), "[build].output_dir"
    )
    context = project_path(
        config,
        build.get("context", "."),
        "[build].context",
        allow_root=True,
        kind="directory",
    )
    dockerfile = project_path(
        config,
        build.get("dockerfile", "Dockerfile"),
        "[build].dockerfile",
        kind="file",
    )
    platforms = build.get("platforms", ["linux/amd64"])
    version = project_version(config)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    try:
        run(["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True)
    except subprocess.CalledProcessError:
        print("Error: Docker is not available", file=sys.stderr)
        return 1

    subprocess.run(
        [
            "docker", "run", "--rm", "--privileged",
            "multiarch/qemu-user-static", "--reset", "-p", "yes",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    build_args = dict(build.get("args", {}))
    build_args.setdefault("VERSION", version)
    build_args.setdefault("GIT_SHA", git_revision(context))
    if args.include_appimage:
        build_args["INCLUDE_APPIMAGE"] = "1"

    command = [
        "docker", "buildx", "build",
        "--platform", ",".join(platforms),
    ]
    for key, value in build_args.items():
        command.extend(["--build-arg", f"{key}={value}"])
    command.extend([
        "--output", f"type=local,dest={output_dir}",
        "--progress=plain",
        "-f", str(dockerfile),
        str(context),
    ])

    print(f"\nBuilding {config.project['id']} for {', '.join(platforms)} ...\n")
    run(command)
    flatten_output(output_dir)

    git_pkgbuild = config.section("aur").get("git_pkgbuild")
    if git_pkgbuild:
        source = config.root / git_pkgbuild
        if source.is_file():
            shutil.copy2(source, output_dir / source.name)

    write_checksums(output_dir)

    print(f"\nPackages ({output_dir}):")
    for artifact in sorted(output_dir.iterdir()):
        if artifact.is_file():
            print(f"  {artifact.name:<45s} {artifact.stat().st_size:>8,} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
