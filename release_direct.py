#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Upload configured release artifacts to Cloudflare R2.

Usage:
    ./release_direct.py

Environment:
    AWS_ACCESS_KEY_ID         Cloudflare R2 access key
    AWS_SECRET_ACCESS_KEY     Cloudflare R2 secret key
    AWS_ENDPOINT_URL          R2 endpoint
    AWS_BUCKET                R2 bucket name
"""

import argparse
import atexit
import sys
from pathlib import Path

from release_lib import (
    acquire_r2_lock,
    compute_hashes,
    list_prefix,
    load_config,
    load_env_file,
    project_path,
    project_version,
    release_artifacts,
    upload_file,
    upload_text,
    validate_config,
)


def find_artifacts(config, version):
    dist = project_path(
        config,
        config.section("build").get("output_dir", "dist"),
        "[build].output_dir",
    )
    artifacts = release_artifacts(config, version)
    missing = [
        artifact["name"]
        for artifact in artifacts
        if not (dist / artifact["name"]).is_file()
    ]
    if missing:
        print("Missing release artifact(s): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)
    return [(artifact, dist / artifact["name"]) for artifact in artifacts]


def upload_artifacts(config, artifacts):
    prefix = config.section("hosting").get("release_prefix", "releases")
    existing = set(list_prefix(prefix))
    for artifact, path in artifacts:
        key = f"{prefix}/{artifact['name']}"
        if key in existing:
            print(f"  {key} (unchanged)")
        else:
            upload_file(path, key)
            existing.add(key)

        checksum_key = key + ".sha256"
        if checksum_key in existing:
            print(f"  {checksum_key} (unchanged)")
            continue
        checksum = compute_hashes(path)[2]
        upload_text(f"{checksum}  {artifact['name']}\n", checksum_key)
        existing.add(checksum_key)


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
    validate_config(config, "direct")
    version = project_version(config)
    configured = release_artifacts(config, version)
    if not configured:
        print("No [release].artifacts configured; skipping direct release")
        return 0

    artifacts = find_artifacts(config, version)
    prefix = config.section("hosting").get("release_prefix", "releases")
    release_lock = acquire_r2_lock(f"{prefix}/.direct.lock")
    atexit.register(release_lock)
    print(f"Uploading {len(artifacts)} release artifact(s) to R2 ...")
    upload_artifacts(config, artifacts)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
