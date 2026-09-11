#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Build and publish a DNF/RPM repository to Cloudflare R2.

Uploads under "rpm/" prefix (including pubkey.gpg).

Usage:
    ./release_rpm.py              # build repo, sign, upload to R2
    ./release_rpm.py --dry-run    # build repo locally, skip upload
    ./release_rpm.py --serve      # build repo and serve via HTTP for testing

Environment:
    AWS_ACCESS_KEY_ID         Cloudflare R2 access key
    AWS_SECRET_ACCESS_KEY     Cloudflare R2 secret key
    AWS_ENDPOINT_URL          R2 endpoint
    AWS_BUCKET                R2 bucket name
    GPG_KEY_ID                GPG key ID for signing
    GPG_PASSPHRASE            passphrase (optional, only if key has one)
"""

import argparse
import atexit
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from release_lib import (
    acquire_r2_lock,
    check_tools,
    delete_keys,
    download_prefix,
    load_config,
    load_env_file,
    list_prefix,
    project_path,
    project_version,
    reject_published_release,
    release_marker_key,
    upload_tree,
    validate_config,
    write_release_marker,
)


def find_rpms(dist, package_name, version, release, platforms):
    arch_map = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
    rpms = [
        dist / f"{package_name}-{version}-{release}.{arch_map[platform]}.rpm"
        for platform in platforms
    ]
    missing = [path.name for path in rpms if not path.is_file()]
    if missing:
        print("Missing required .rpm file(s): " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)
    return rpms


def build_repo(root_dir, rpms, prefix, package_name):
    rpm_dir = root_dir / prefix
    rpm_dir.mkdir(parents=True, exist_ok=True)

    for path in rpm_dir.iterdir():
        if (
            path.is_file()
            and path.suffix == ".rpm"
            and path.name.startswith(f"{package_name}-")
        ):
            path.unlink()

    current_rpms = []
    for rpm in rpms:
        destination = rpm_dir / rpm.name
        shutil.copy2(rpm, destination)
        current_rpms.append(destination)

    gpg_key = os.environ.get("GPG_KEY_ID")
    if gpg_key:
        sign_rpms(current_rpms, gpg_key)

    subprocess.run(
        ["createrepo_c", str(rpm_dir)],
        check=True,
    )


def pre_cache_gpg_key(key_id, passphrase=None):
    """Sign a dummy to load the key into gpg-agent so rpmsign never prompts."""
    cmd = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback"]
    sp_args = {}
    if passphrase:
        cmd += ["--passphrase-fd", "0"]
        sp_args["input"] = passphrase.encode()
    cmd += ["--sign", "--local-user", key_id, "-o", "/dev/null", "/dev/null"]
    subprocess.run(cmd, check=True, **sp_args)


def sign_rpms(rpms, key_id):
    cmd = ["rpmsign", "--addsign", "--define", f"_gpg_name {key_id}"]
    for rpm in sorted(rpms):
        subprocess.run(cmd + [str(rpm)], check=True)


def rpm_upload_order(relative):
    """Publish RPM payloads before the repository entry-point metadata."""
    if relative.suffix == ".rpm" or relative.name == "pubkey.gpg":
        return 0
    if relative.name == "repomd.xml.asc":
        return 2
    if relative.name == "repomd.xml":
        return 3
    return 1


def rpm_upload_headers(relative):
    if relative.suffix == ".rpm":
        return {"CacheControl": "public, max-age=31536000, immutable"}
    return {"CacheControl": "no-cache"}


def gpg_sign_repomd(repomd_path, key_id, passphrase=None):
    cmd = ["gpg", "--batch", "--yes"]
    sp_args = {}
    if key_id:
        cmd += ["--local-user", key_id]
    if passphrase:
        cmd += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
        sp_args["input"] = passphrase.encode()

    asc_path = repomd_path.parent / "repomd.xml.asc"
    subprocess.run(
        cmd + ["--detach-sign", "--armor", "-o", str(asc_path), str(repomd_path)],
        check=True,
        **sp_args,
    )


def export_pubkey(key_id, repo_dir, prefix):
    result = subprocess.run(
        ["gpg", "--export", "--armor", key_id],
        capture_output=True,
        text=True,
        check=True,
    )
    (repo_dir / prefix / "pubkey.gpg").write_text(result.stdout)


def serve_repo(repo_dir, *, prefix, package_name):
    os.chdir(repo_dir)

    host = "0.0.0.0"
    port = 8080
    print(f"Serving repository on http://{host}:{port}")
    print("Test with:\n")
    print("  docker run --network=host --rm -it fedora:latest bash")
    print("  # Inside container:")
    print("  dnf install -y dnf-plugins-core")
    print("  tee /etc/yum.repos.d/promptr.repo <<'EOF'")
    print("[promptr]")
    print(f"baseurl=http://localhost:{port}/{prefix}")
    print("gpgcheck=0")
    print("enabled=1")
    print("EOF")
    print(f"  dnf install {package_name}")
    print()
    print("Press Ctrl+C to stop.")

    server = http.server.HTTPServer(
        (host, port),
        http.server.SimpleHTTPRequestHandler,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Publish RPM repo to R2")
    parser.add_argument(
        "--env", metavar="PATH", help="Load env vars from file (KEY=VALUE per line)"
    )
    parser.add_argument(
        "--project-root", default=".", help="Project directory containing release.toml"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Build repo locally, skip upload"
    )
    mode.add_argument(
        "--serve", action="store_true", help="Build repo and serve via HTTP for testing"
    )
    parser.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="Allow a production upload without GPG signatures",
    )
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    config = load_config(Path(args.project_root) / "release.toml")
    validate_config(config, "rpm")
    project = config.project
    build = config.section("build")
    rpm = config.section("rpm")
    hosting = config.section("hosting")
    prefix = hosting.get("rpm_prefix", "rpm")
    package_name = rpm.get("package_name", project["id"])
    dist = project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
    version = project_version(config)
    release = str(rpm.get("release", "1"))
    platforms = build.get("platforms", ["linux/amd64"])

    check_tools("createrepo_c", "gpg", "rpmsign")

    gpg_key = os.environ.get("GPG_KEY_ID")
    if gpg_key and os.environ.get("GPG_PASSPHRASE"):
        pre_cache_gpg_key(gpg_key, os.environ["GPG_PASSPHRASE"])

    rpms = find_rpms(dist, package_name, version, release, platforms)
    print(f"Found {len(rpms)} package(s):")
    for r in rpms:
        print(f"  {r.name}")

    gpg_key = os.environ.get("GPG_KEY_ID")
    publishing = not args.dry_run and not args.serve
    if publishing and not gpg_key and not args.allow_unsigned:
        print(
            "Error: GPG_KEY_ID is required for publishing; "
            "pass --allow-unsigned to override",
            file=sys.stderr,
        )
        return 1

    repo = tempfile.mkdtemp(prefix=f"{package_name}-rpm-")
    repo_path = Path(repo)
    keep = args.dry_run
    if not keep:
        atexit.register(shutil.rmtree, repo, ignore_errors=True)
    existing_package_keys = []
    existing_marker_keys = []
    current_package_keys = {f"{prefix}/{rpm.name}" for rpm in rpms}
    if not args.dry_run and not args.serve:
        release_lock = acquire_r2_lock(f"{prefix}/.publish.lock")
        atexit.register(release_lock)
        existing_package_keys = list_prefix(prefix, suffix=".rpm")
        marker_prefix = f"{prefix}/.published/{package_name}"
        existing_marker_keys = list_prefix(marker_prefix, suffix=".published")
        current_package_keys = {
            f"{prefix}/{rpm.name}" for rpm in rpms
        }
        try:
            reject_published_release(
                [
                    key
                    for key in existing_package_keys
                    if key.startswith(f"{prefix}/{package_name}-")
                ]
                + existing_marker_keys,
                current_package_keys
                | {release_marker_key(prefix, package_name, version)},
                package_name,
                version,
            )
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        print("Reading existing shared RPM payloads from R2 ...")
        existing = download_prefix(prefix, repo_path / prefix, suffix=".rpm")
        print(f"Found {len(existing)} existing RPM(s).")

    print(f"\nBuilding RPM repository in {repo} ...")
    build_repo(repo_path, rpms, prefix, package_name)

    if args.serve:
        serve_repo(repo_path, prefix=prefix, package_name=package_name)
        return

    if gpg_key:
        repomd = repo_path / prefix / "repodata" / "repomd.xml"
        gpg_sign_repomd(repomd, gpg_key, os.environ.get("GPG_PASSPHRASE"))
        export_pubkey(gpg_key, repo_path, prefix)
    else:
        print("Note: GPG_KEY_ID not set, skipping signing.", file=sys.stderr)

    if args.dry_run:
        print("Dry run — skipping upload.")
        print(f"Repository at: {repo}")
    else:
        print("Uploading to R2 ...")
        upload_tree(
            repo_path,
            order=rpm_upload_order,
            extra_args=rpm_upload_headers,
            skip_existing=existing_package_keys,
        )
        stale_package_keys = sorted(
            key
            for key in existing_package_keys
            if key.startswith(f"{prefix}/{package_name}-")
            and key not in current_package_keys
        )
        if stale_package_keys:
            print(f"Removing {len(stale_package_keys)} old {package_name} package(s) ...")
            delete_keys(stale_package_keys)
        marker = write_release_marker(prefix, package_name, version)
        print(f"  {marker}")
        print("Done.")

    print(f"\nRepository built for {package_name} {version}.")


if __name__ == "__main__":
    sys.exit(main())
