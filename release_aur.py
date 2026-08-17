#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Push to AUR and upload release archives to Cloudflare R2.

Usage:
    python3 release-aur.py --type git     # initial submit, then only on dep changes
    python3 release-aur.py --type bin     # every release (uploads .tar.gz + pushes PKGBUILD)
    python3 release-aur.py --type both

Environment:
    AWS_ACCESS_KEY_ID         Cloudflare R2 access key
    AWS_SECRET_ACCESS_KEY     Cloudflare R2 secret key
    AWS_ENDPOINT_URL          R2 endpoint
    AWS_BUCKET                R2 bucket name
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from release_lib import load_config, load_env_file, project_version, upload_file

# Arch CARCH -> our filename arch suffix
_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}

BIN_PKGBUILD_TEMPLATE = """\
# Maintainer: {maintainer}
pkgname={package_name}
pkgver={version}
pkgrel=1
pkgdesc="{description}"
arch=('x86_64' 'aarch64')
url="{url}"
license=('{license}')
depends=({depends})

source_x86_64=("{project_id}-${{pkgver}}-x86_64.tar.gz::{release_url}/{project_id}_${{pkgver}}_amd64.tar.gz")
sha256sums_x86_64=('{sha256_amd64}')

source_aarch64=("{project_id}-${{pkgver}}-aarch64.tar.gz::{release_url}/{project_id}_${{pkgver}}_arm64.tar.gz")
sha256sums_aarch64=('{sha256_arm64}')

package() {{
  bsdtar -xf "${{srcdir}}/{project_id}-${{pkgver}}-${{CARCH}}.tar.gz" -C "${{pkgdir}}"
}}
"""


def run(cmd, **kwargs):
    subprocess.run(cmd, shell=True, check=True, **kwargs)


def parse_bash_array(val):
    val = val.strip()
    if not val.startswith("(") or not val.endswith(")"):
        return None
    inner = val[1:-1]
    parts = []
    current = ""
    in_quote = False
    for ch in inner:
        if ch == "'" or ch == '"':
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            if current:
                parts.append(current)
                current = ""
        else:
            current += ch
    if current:
        parts.append(current)
    return parts or None


def strip_quotes(val):
    val = val.strip()
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def generate_srcinfo(pkgbuild_text):
    pkgbase = None
    pkgname = None
    lines_out = []
    in_func = 0

    for line in pkgbuild_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("#"):
            continue
        if not stripped:
            continue

        if "()" in stripped and "{" in stripped:
            in_func = 1
            continue
        if in_func > 0:
            in_func += stripped.count("{") - stripped.count("}")
            continue

        if "=" in stripped:
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip()

            if key == "pkgname":
                pkgname = val
                if pkgbase is None:
                    pkgbase = val
                continue

            arr = parse_bash_array(val)
            if arr:
                for item in arr:
                    lines_out.append(f"\t{key} = {strip_quotes(item)}")
            else:
                lines_out.append(f"\t{key} = {strip_quotes(val)}")

    header = f"pkgbase = {pkgbase or 'promptr-git'}\n"
    lines_out.append(f"\npkgname = {pkgname or 'promptr-git'}")
    return header + "\n".join(lines_out) + "\n"


def clone_or_pull(repo_name, workdir, aur_host, aur_user):
    repo_path = workdir / repo_name
    aur_url = f"{aur_user}@{aur_host}:{repo_name}.git"

    if (repo_path / ".git").exists():
        run(f"git -C {repo_path} pull --rebase", cwd=workdir)
    else:
        run(f"git clone {aur_url} {repo_path}", cwd=workdir)
    return repo_path


def push_aur(repo_name, pkgbuild_text, *, aur_host, aur_user, maintainer,
             version):
    with tempfile.TemporaryDirectory(prefix="aur-") as tmp:
        workdir = Path(tmp)
        repo = clone_or_pull(repo_name, workdir, aur_host, aur_user)

        pkgbuild_text = pkgbuild_text.rstrip("\n") + "\n"
        srcinfo = generate_srcinfo(pkgbuild_text)

        old_pkg = (repo / "PKGBUILD").read_text() if (repo / "PKGBUILD").exists() else ""
        old_src = (repo / ".SRCINFO").read_text() if (repo / ".SRCINFO").exists() else ""

        if old_pkg.rstrip() == pkgbuild_text.rstrip() and old_src.rstrip() == srcinfo.rstrip():
            print(f"  -> AUR {repo_name}: no changes, skipping")
            return

        (repo / "PKGBUILD").write_text(pkgbuild_text)
        (repo / ".SRCINFO").write_text(srcinfo)

        run(f'git -C {repo} add PKGBUILD .SRCINFO')
        subprocess.run(
            f'git -C {repo} commit --author "{maintainer}" '
            f'-m "Release {version}"',
            shell=True,
        )
        run(f"git -C {repo} push -u origin HEAD:master")


def upload_releases(config, version):
    dist = config.root / config.section("build").get("output_dir", "dist")
    release_prefix = config.section("hosting").get("release_prefix", "releases")
    project_id = config.project["id"]

    for deb_arch in _ARCH_MAP.values():
        tarball = dist / f"{project_id}_{version}_{deb_arch}.tar.gz"
        if not tarball.is_file():
            print(f"Error: {tarball} not found", file=sys.stderr)
            sys.exit(1)
        key = f"{release_prefix}/{tarball.name}"
        upload_file(tarball, key)


def release_git(config, version, *, aur_host, aur_user, maintainer):
    aur = config.section("aur")
    template = aur.get("git_pkgbuild")
    if not template:
        print("No [aur].git_pkgbuild configured; skipping git package")
        return

    pkgbuild = (config.root / template).read_text()
    pkgbuild = re.sub(r'^pkgver=.*$', f'pkgver={version}', pkgbuild,
                      flags=re.MULTILINE)
    package_name = aur.get("git_package", f"{config.project['id']}-git")
    push_aur(
        package_name,
        pkgbuild,
        aur_host=aur_host,
        aur_user=aur_user,
        maintainer=maintainer,
        version=version,
    )
    print(f"  -> AUR: {package_name} updated")


def release_bin(config, version, *, aur_host, aur_user, maintainer):
    aur = config.section("aur")
    project = config.project
    dist = config.root / config.section("build").get("output_dir", "dist")
    project_id = project["id"]
    release_url = (
        config.section("hosting").get("public_base_url", "").rstrip("/")
        + "/"
        + config.section("hosting").get("release_prefix", "releases")
    )
    package_name = aur.get("binary_package", f"{project_id}-bin")
    description = project.get("description", project.get("name", project_id))
    depends = " ".join(f"'{dependency}'" for dependency in aur.get("depends", []))
    checksums = {}
    for car_ch, deb_arch in _ARCH_MAP.items():
        tarball = dist / f"{project_id}_{version}_{deb_arch}.tar.gz"
        print(f"  Checksum for {car_ch}...")
        sum_file = dist / f"{tarball.name}.sha256"
        if sum_file.is_file():
            line = sum_file.read_text().strip()
            checksums[car_ch] = line.split()[0]
        elif tarball.is_file():
            print(f"  (computing from file)", file=sys.stderr)
            checksums[car_ch] = hashlib.sha256(tarball.read_bytes()).hexdigest()
        else:
            print(f"Error: {tarball} not found", file=sys.stderr)
            sys.exit(1)

    pkgbuild = BIN_PKGBUILD_TEMPLATE.format(
        maintainer=maintainer,
        package_name=package_name,
        version=version,
        description=description,
        url=project.get("homepage", project.get("repository", "")),
        license=project.get("license", "custom"),
        project_id=project_id,
        release_url=release_url,
        depends=depends,
        sha256_amd64=checksums["x86_64"],
        sha256_arm64=checksums["aarch64"],
    )
    push_aur(
        package_name,
        pkgbuild,
        aur_host=aur_host,
        aur_user=aur_user,
        maintainer=maintainer,
        version=version,
    )
    print(f"  -> AUR: {package_name} updated")


def main():
    parser = argparse.ArgumentParser(description="Push to AUR and upload releases")
    parser.add_argument("--env", metavar="PATH",
                        help="Load env vars from file (KEY=VALUE per line)")
    parser.add_argument("--project-root", default=".",
                        help="Project directory containing release.toml")
    parser.add_argument(
        "--type",
        choices=["git", "bin", "both"],
        default="both",
        help="Package type to release (default: both)",
    )
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    config = load_config(Path(args.project_root) / "release.toml")
    version = project_version(config)
    hosting = config.section("hosting")
    aur_hosting = hosting.get("aur", {})
    aur_host = aur_hosting.get("host", "aur.archlinux.org")
    aur_user = aur_hosting.get("ssh_user", "aur")
    maintainer = aur_hosting.get(
        "maintainer", config.project.get("maintainer", ""))

    run("ssh -o StrictHostKeyChecking=accept-new -T "
        f"{aur_user}@{aur_host} 2>&1 | grep -q username || true",
        capture_output=True)

    if args.type in ("git", "both"):
        release_git(
            config,
            version,
            aur_host=aur_host,
            aur_user=aur_user,
            maintainer=maintainer,
        )

    if args.type in ("bin", "both"):
        print("Uploading release archives to R2 ...")
        upload_releases(config, version)
        release_bin(
            config,
            version,
            aur_host=aur_host,
            aur_user=aur_user,
            maintainer=maintainer,
        )


if __name__ == "__main__":
    main()
