#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# ///

"""Publish a project formula to a Homebrew tap.

Usage:
    ./release_homebrew.py

The formula refers to publicly-hosted archives. This script updates the tap
only; it does not upload release artifacts.
"""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from release_lib import (
    check_tools,
    compute_hashes,
    load_config,
    load_env_file,
    project_path,
    project_version,
    release_artifacts,
    run,
    validate_config,
)


def ruby_string(value):
    """Return a safely quoted Ruby string."""
    return json.dumps(value)


def formula_class_name(formula):
    words = re.findall(r"[a-z0-9]+", formula)
    return "".join(word[:1].upper() + word[1:] for word in words)


def render_url(url, version):
    try:
        rendered = url.format(version=version)
    except (IndexError, KeyError, ValueError) as error:
        raise ValueError(f"invalid Homebrew archive URL {url!r}: {error}") from error
    if "{" in rendered or "}" in rendered:
        raise ValueError(
            "Homebrew archive URLs may only contain the {version} placeholder"
        )
    return rendered


def find_archives(config, version):
    dist = project_path(
        config,
        config.section("build").get("output_dir", "dist"),
        "[build].output_dir",
    )
    artifacts = {
        artifact["name"]: artifact for artifact in release_artifacts(config, version)
    }
    archives = []
    missing = []
    for archive in config.section("homebrew")["archives"]:
        name = archive["artifact"].format(version=version)
        path = dist / name
        if name not in artifacts or not path.is_file():
            missing.append(name)
        else:
            archives.append((archive, path))
    if missing:
        print("Missing Homebrew archive(s): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)
    return archives


def generate_formula(config, version, archives):
    homebrew = config.section("homebrew")
    project = config.project
    formula = homebrew["formula"]
    binary = homebrew.get(
        "binary", config.section("package").get("binary", project["id"])
    )
    lines = [
        f"class {formula_class_name(formula)} < Formula",
        f"  desc {ruby_string(project.get('description', project.get('name', formula)))}",
        f"  homepage {ruby_string(project.get('homepage', project.get('repository', '')))}",
        f"  version {ruby_string(version)}",
    ]

    for archive, path in archives:
        url = render_url(archive["url"], version)
        checksum = compute_hashes(path)[2]
        architecture = archive["architecture"]
        if architecture == "universal":
            lines.extend(
                [
                    f"  url {ruby_string(url)}",
                    f"  sha256 {ruby_string(checksum)}",
                ]
            )
        else:
            method = "on_intel" if architecture == "x86_64" else "on_arm"
            lines.extend(
                [
                    f"  {method} do",
                    f"    url {ruby_string(url)}",
                    f"    sha256 {ruby_string(checksum)}",
                    "  end",
                ]
            )

    lines.extend(
        [
            "",
            "  def install",
            f"    bin.install {ruby_string(binary)}",
        ]
    )
    for asset in homebrew.get("assets", []):
        lines.append(
            f"    {asset['destination']}.install {ruby_string(asset['source'])}"
        )
    lines.extend(["  end", "end", ""])
    return "\n".join(lines)


def tap_remote(homebrew):
    remote = homebrew.get("remote")
    if remote:
        return remote
    owner, repository = homebrew["tap"].split("/", 1)
    return f"git@github.com:{owner}/homebrew-{repository}.git"


def update_tap(config, version, formula_text):
    homebrew = config.section("homebrew")
    branch = homebrew.get("branch", "main")
    formula = homebrew["formula"]
    with tempfile.TemporaryDirectory(prefix="homebrew-") as directory:
        workdir = Path(directory)
        tap = workdir / "tap"
        run(
            [
                "git",
                "clone",
                "--branch",
                branch,
                "--",
                tap_remote(homebrew),
                str(tap),
            ]
        )
        formula_path = tap / "Formula" / f"{formula}.rb"
        formula_path.parent.mkdir(parents=True, exist_ok=True)
        old_formula = formula_path.read_text() if formula_path.exists() else ""
        if old_formula == formula_text:
            print(f"  -> Homebrew: {formula} unchanged")
            return

        formula_path.write_text(formula_text)
        run(["git", "-C", str(tap), "add", str(formula_path.relative_to(tap))])
        run(["git", "-C", str(tap), "commit", "-m", f"Release {version}"])
        run(["git", "-C", str(tap), "push", "origin", f"HEAD:{branch}"])
        print(f"  -> Homebrew: {formula} updated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", metavar="PATH", help="Load env vars from file (KEY=VALUE per line)"
    )
    parser.add_argument(
        "--project-root", default=".", help="Project directory containing release.toml"
    )
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    config = load_config(Path(args.project_root) / "release.toml")
    if not config.section("homebrew"):
        print("No [homebrew] configured; skipping Homebrew release")
        return 0
    validate_config(config, "homebrew")
    check_tools("git")
    version = project_version(config)
    archives = find_archives(config, version)
    formula = generate_formula(config, version, archives)
    update_tap(config, version, formula)
    return 0


if __name__ == "__main__":
    sys.exit(main())
