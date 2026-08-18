#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Build and publish an apt repository to Cloudflare R2.

Uploads under "apt/" prefix (including pubkey.gpg).

Usage:
    ./release-apt.py              # build repo, sign, upload to R2
    ./release-apt.py --dry-run    # build repo locally, skip upload
    ./release-apt.py --serve      # build repo and serve via HTTP for testing

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
import email.parser
import gzip
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from release_lib import (
    acquire_r2_lock,
    check_tools,
    compute_hashes,
    download_optional,
    load_config,
    load_env_file,
    project_path,
    project_version,
    upload_tree,
    validate_config,
)


def parse_packages(content):
    """Return package stanzas keyed by package and architecture."""
    stanzas = {}
    for raw in content.split("\n\n"):
        stanza = raw.strip()
        if not stanza:
            continue
        fields = {}
        for line in stanza.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        identity = (
            fields.get("Package"),
            fields.get("Architecture"),
        )
        if all(identity) and fields.get("Version"):
            stanzas[identity] = stanza
    return stanzas


def merge_packages(existing, current):
    """Merge package indexes, with the current project's version taking precedence."""
    merged = parse_packages(existing)
    merged.update(parse_packages(current))
    return "\n\n".join(
        merged[key] for key in sorted(merged)
    ) + ("\n" if merged else "")


def load_existing_packages(prefix, suite, component, archs, destination):
    """Fetch shared indexes without downloading any package payloads."""
    indexes = {}
    for arch in archs:
        key_base = (
            f"{prefix}/dists/{suite}/{component}/binary-{arch}/Packages"
        )
        plain_path = destination / f"Packages-{arch}"
        gzip_path = destination / f"Packages-{arch}.gz"
        if download_optional(key_base, plain_path):
            indexes[arch] = plain_path.read_text()
        elif download_optional(key_base + ".gz", gzip_path):
            with gzip.open(gzip_path, "rt") as file:
                indexes[arch] = file.read()
    return indexes

def parse_control(deb_path):
    result = subprocess.run(
        ["dpkg-deb", "-f", str(deb_path)],
        capture_output=True, text=True, check=True,
    )
    parser = email.parser.HeaderParser()
    msg = parser.parsestr(result.stdout)
    return dict(msg)


def find_debs(dist, package_name):
    debs = sorted(dist.glob(f"{package_name}_*.deb"))
    if not debs:
        print(f"No {package_name} .deb files found in {dist}/", file=sys.stderr)
        sys.exit(1)
    return debs


def apt_upload_order(relative):
    """Publish payloads first and client entry-point metadata last."""
    name = relative.name
    parts = relative.parts
    if "pool" in parts or name == "pubkey.gpg":
        return 0
    if name in {"Packages", "Packages.gz"}:
        return 1
    if name == "Release":
        return 2
    if name == "Release.gpg":
        return 3
    if name == "InRelease":
        return 4
    return 1


def apt_upload_headers(relative):
    if "pool" in relative.parts:
        return {"CacheControl": "public, max-age=31536000, immutable"}
    return {"CacheControl": "no-cache"}


def build_repo(
    root_dir,
    debs,
    *,
    prefix,
    component,
    suite,
    archs,
    package_name,
    origin,
    label,
    existing_packages=None,
):
    prefix_dir = root_dir / prefix
    pool_dir = prefix_dir / "pool" / component / package_name[0] / package_name
    pool_dir.mkdir(parents=True, exist_ok=True)

    for deb in debs:
        shutil.copy2(deb, pool_dir / deb.name)

    for arch in archs:
        bins_dir = prefix_dir / "dists" / suite / component / f"binary-{arch}"
        bins_dir.mkdir(parents=True, exist_ok=True)

        arch_debs = [pool_dir / d.name for d in debs
                     if d.name.endswith(f"_{arch}.deb")]
        if not arch_debs:
            continue

        entries = []
        for deb_path in sorted(arch_debs):
            control = parse_control(deb_path)
            rel_name = (
                f"pool/{component}/{package_name[0]}/{package_name}/"
                f"{deb_path.name}"
            )
            md5, sha1, sha256, size = compute_hashes(deb_path)

            control["Filename"] = rel_name
            control["Size"] = str(size)
            control["MD5sum"] = md5
            control["SHA1"] = sha1
            control["SHA256"] = sha256

            entries.append("\n".join(
                f"{k}: {v}" for k, v in control.items()
            ))

        content = "\n\n".join(entries) + "\n"
        if existing_packages and arch in existing_packages:
            content = merge_packages(existing_packages[arch], content)
        (bins_dir / "Packages").write_text(content)
        with gzip.open(bins_dir / "Packages.gz", "wt") as fp:
            fp.write(content)

    dist_dir = prefix_dir / "dists" / suite
    build_release(
        dist_dir,
        suite=suite,
        component=component,
        archs=archs,
        origin=origin,
        label=label,
    )


def build_release(dist_dir, *, suite, component, archs, origin, label):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    lines = [
        f"Origin: {origin}",
        f"Label: {label}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Architectures: {' '.join(archs)}",
        f"Components: {component}",
        f"Description: {label} apt repository",
        f"Date: {now}",
    ]

    meta = [f for f in sorted(dist_dir.rglob("*")) if f.is_file()]

    md5_lines = []
    sha1_lines = []
    sha256_lines = []

    for fpath in meta:
        rel = str(fpath.relative_to(dist_dir))
        md5, sha1, sha256, size = compute_hashes(fpath)
        md5_lines.append(f" {md5} {size} {rel}")
        sha1_lines.append(f" {sha1} {size} {rel}")
        sha256_lines.append(f" {sha256} {size} {rel}")

    if md5_lines:
        lines.append("MD5Sum:")
        lines.extend(md5_lines)
    if sha1_lines:
        lines.append("SHA1:")
        lines.extend(sha1_lines)
    if sha256_lines:
        lines.append("SHA256:")
        lines.extend(sha256_lines)

    (dist_dir / "Release").write_text("\n".join(lines) + "\n")


def gpg_sign(release_path, key_id, passphrase=None):
    dist_dir = release_path.parent
    cmd = ["gpg", "--batch", "--yes"]
    sp_args = {}
    if key_id:
        cmd += ["--local-user", key_id]
    if passphrase:
        cmd += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
        sp_args["input"] = passphrase.encode()

    subprocess.run(
        cmd + ["--clearsign", "--armor", "-o",
               str(dist_dir / "InRelease"), str(release_path)],
        check=True, **sp_args,
    )
    subprocess.run(
        cmd + ["--detach-sign", "--armor", "-o",
               str(dist_dir / "Release.gpg"), str(release_path)],
        check=True, **sp_args,
    )


def export_pubkey(key_id, repo_dir, prefix):
    result = subprocess.run(
        ["gpg", "--export", "--armor", key_id],
        capture_output=True, text=True, check=True,
    )
    (repo_dir / prefix / "pubkey.gpg").write_text(result.stdout)


def serve_repo(repo_dir, *, prefix, suite, component, package_name):
    os.chdir(repo_dir)

    host = "0.0.0.0"
    port = 8080
    print(f"Serving repository on http://{host}:{port}")
    print("Test with:\n")
    print("  docker run --network=host --rm -it debian:bookworm bash")
    print("  # Inside container:")
    print(f"  echo 'deb [trusted=yes] http://localhost:{port}/{prefix} "
          f"{suite} {component}' \\")
    print(f"    > /etc/apt/sources.list.d/{package_name}.list")
    print(f"  apt update && apt install {package_name}")
    print()
    print("Press Ctrl+C to stop.")

    server = http.server.HTTPServer(
        (host, port), http.server.SimpleHTTPRequestHandler,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Publish apt repo to R2")
    parser.add_argument("--env", metavar="PATH",
                        help="Load env vars from file (KEY=VALUE per line)")
    parser.add_argument("--project-root", default=".",
                        help="Project directory containing release.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Build repo locally, skip upload")
    mode.add_argument("--serve", action="store_true",
                      help="Build repo and serve via HTTP for testing")
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    config = load_config(Path(args.project_root) / "release.toml")
    project = config.project
    build = config.section("build")
    apt = config.section("deb")
    hosting = config.section("hosting")
    apt_hosting = hosting.get("apt", {})
    prefix = hosting.get("apt_prefix", "apt")
    suite = apt_hosting.get("suite", "stable")
    component = apt_hosting.get("component", "main")
    archs = tuple(apt.get("architectures", ("amd64",)))
    package_name = apt.get("package_name", project["id"])
    dist = project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
    version = project_version(config)

    check_tools("dpkg-deb", "dpkg", "gpg")

    debs = find_debs(dist, package_name)
    print(f"Found {len(debs)} package(s):")
    for d in debs:
        print(f"  {d.name}")

    repo = tempfile.mkdtemp(prefix=f"{package_name}-apt-")
    repo_path = Path(repo)
    keep = args.dry_run  # keep repo for inspection
    if not keep:
        atexit.register(shutil.rmtree, repo, ignore_errors=True)
    existing = {}
    existing_dir = None
    if not args.dry_run and not args.serve:
        release_lock = acquire_r2_lock(f"{prefix}/.publish.lock")
        atexit.register(release_lock)
        print("Reading existing shared package indexes from R2 ...")
        existing_dir = Path(tempfile.mkdtemp(prefix=f"{package_name}-apt-existing-"))
        atexit.register(shutil.rmtree, existing_dir, ignore_errors=True)
        existing = load_existing_packages(
            prefix, suite, component, archs, existing_dir,
        )
    print(f"\nBuilding apt repository in {repo} ...")
    build_repo(
        repo_path,
        debs,
        prefix=prefix,
        component=component,
        suite=suite,
        archs=archs,
        package_name=package_name,
        origin=apt_hosting.get("origin", project["id"]),
        label=apt_hosting.get("label", project.get("name", project["id"])),
        existing_packages=existing,
    )

    if args.serve:
        serve_repo(
            repo_path,
            prefix=prefix,
            suite=suite,
            component=component,
            package_name=package_name,
        )
        return

    gpg_key = os.environ.get("GPG_KEY_ID")
    if gpg_key:
        release_path = repo_path / prefix / "dists" / suite / "Release"
        gpg_sign(release_path, gpg_key,
                 os.environ.get("GPG_PASSPHRASE"))
        export_pubkey(gpg_key, repo_path, prefix)
    else:
        print("Note: GPG_KEY_ID not set, skipping signing.",
              file=sys.stderr)

    if args.dry_run:
        print("Dry run — skipping upload.")
        print(f"Repository at: {repo}")
    else:
        print("Uploading to R2 ...")
        upload_tree(repo_path, order=apt_upload_order, extra_args=apt_upload_headers)
        print("Done.")

    print(f"\nRepository built for {package_name} {version} ({suite}).")


if __name__ == "__main__":
    main()
