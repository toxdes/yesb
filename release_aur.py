#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Push to AUR and upload release archives to Cloudflare R2.

Usage:
    ./release_aur.py --type git     # initial submit, then only on dep changes
    ./release_aur.py --type bin     # every release (uploads .tar.gz + pushes PKGBUILD)
    ./release_aur.py --type both

Environment:
    AWS_ACCESS_KEY_ID         Cloudflare R2 access key
    AWS_SECRET_ACCESS_KEY     Cloudflare R2 secret key
    AWS_ENDPOINT_URL          R2 endpoint
    AWS_BUCKET                R2 bucket name
"""

import argparse
import os
import re
import subprocess
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
    run,
    upload_file,
    validate_config,
)

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


def generate_srcinfo(repo, helper_image=None):
    """Generate AUR metadata locally or in an isolated helper image."""
    if helper_image is None:
        command = ["makepkg", "--printsrcinfo"]
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    else:
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--tmpfs",
            f"/work:rw,noexec,nosuid,size=64m,uid={os.getuid()},gid={os.getgid()}",
            "--mount",
            f"type=bind,src={repo},dst=/input,readonly",
            "--workdir",
            "/work",
            "--env",
            "HOME=/tmp",
            helper_image,
            "sh",
            "-c",
            "cp -- /input/PKGBUILD /work/PKGBUILD && makepkg --printsrcinfo",
        ]
        print("  $ " + " ".join(str(part) for part in command))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or "no diagnostic output"
            raise RuntimeError(
                f"AUR srcinfo helper failed ({helper_image}): {detail}"
            )
    if not result.stdout.strip():
        raise RuntimeError("makepkg produced an empty .SRCINFO")
    return result.stdout.rstrip("\n") + "\n"


def clone_or_pull(repo_name, workdir, aur_host, aur_user):
    repo_path = workdir / repo_name
    aur_url = f"{aur_user}@{aur_host}:{repo_name}.git"

    if (repo_path / ".git").exists():
        run(["git", "-C", str(repo_path), "pull", "--rebase"], cwd=workdir)
    else:
        run(["git", "clone", "--", aur_url, str(repo_path)], cwd=workdir)
    return repo_path


def push_aur(
    repo_name,
    pkgbuild_text,
    *,
    aur_host,
    aur_user,
    maintainer,
    version,
    srcinfo_helper_image=None,
):
    with tempfile.TemporaryDirectory(prefix="aur-") as tmp:
        workdir = Path(tmp)
        repo = clone_or_pull(repo_name, workdir, aur_host, aur_user)

        old_pkg = (
            (repo / "PKGBUILD").read_text() if (repo / "PKGBUILD").exists() else ""
        )
        old_src = (
            (repo / ".SRCINFO").read_text() if (repo / ".SRCINFO").exists() else ""
        )

        pkgbuild_text = pkgbuild_text.rstrip("\n") + "\n"
        (repo / "PKGBUILD").write_text(pkgbuild_text)
        srcinfo = generate_srcinfo(repo, srcinfo_helper_image)

        if (
            old_pkg.rstrip() == pkgbuild_text.rstrip()
            and old_src.rstrip() == srcinfo.rstrip()
        ):
            print(f"  -> AUR {repo_name}: no changes, skipping")
            return

        (repo / ".SRCINFO").write_text(srcinfo)

        run(["git", "-C", str(repo), "add", "PKGBUILD", ".SRCINFO"])
        run(
            [
                "git",
                "-C",
                str(repo),
                "commit",
                "--author",
                maintainer,
                "-m",
                f"Release {version}",
            ]
        )
        run(["git", "-C", str(repo), "push", "-u", "origin", "HEAD:master"])


def release_archives(config, version):
    """Return a complete, checksummed archive set before any upload begins."""
    dist = project_path(
        config,
        config.section("build").get("output_dir", "dist"),
        "[build].output_dir",
    )
    archives = {}
    missing = []
    for aur_arch, deb_arch in _ARCH_MAP.items():
        tarball = dist / f"{config.project['id']}_{version}_{deb_arch}.tar.gz"
        if not tarball.is_file():
            missing.append(tarball.name)
        else:
            archives[aur_arch] = (
                tarball,
                compute_hashes(tarball)[2],
            )
    if missing:
        print("Missing release archive(s): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)
    return archives


def upload_releases(config, archives):
    release_prefix = config.section("hosting").get("release_prefix", "releases")
    for tarball, _checksum in archives.values():
        key = f"{release_prefix}/{tarball.name}"
        upload_file(tarball, key)


def release_git(
    config,
    version,
    *,
    aur_host,
    aur_user,
    maintainer,
    srcinfo_helper_image=None,
):
    aur = config.section("aur")
    template = aur.get("git_pkgbuild")
    if not template:
        print("No [aur].git_pkgbuild configured; skipping git package")
        return

    template_path = project_path(config, template, "[aur].git_pkgbuild", kind="file")
    pkgbuild = template_path.read_text()
    pkgbuild, replacements = re.subn(
        r"^\s*pkgver\s*=.*$",
        f"pkgver={version}",
        pkgbuild,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise RuntimeError(f"{template_path} must contain one static pkgver assignment")
    package_name = aur.get("git_package", f"{config.project['id']}-git")
    push_aur(
        package_name,
        pkgbuild,
        aur_host=aur_host,
        aur_user=aur_user,
        maintainer=maintainer,
        version=version,
        srcinfo_helper_image=srcinfo_helper_image,
    )
    print(f"  -> AUR: {package_name} updated")


def release_bin(
    config,
    version,
    archives,
    *,
    aur_host,
    aur_user,
    maintainer,
    srcinfo_helper_image=None,
):
    aur = config.section("aur")
    project = config.project
    project_id = project["id"]
    release_url = (
        config.section("hosting").get("public_base_url", "").rstrip("/")
        + "/"
        + config.section("hosting").get("release_prefix", "releases")
    )
    package_name = aur.get("binary_package", f"{project_id}-bin")
    description = project.get("description", project.get("name", project_id))
    depends = " ".join(f"'{dependency}'" for dependency in aur.get("depends", []))
    checksums = {arch: checksum for arch, (_path, checksum) in archives.items()}

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
        srcinfo_helper_image=srcinfo_helper_image,
    )
    print(f"  -> AUR: {package_name} updated")


def main():
    parser = argparse.ArgumentParser(description="Push to AUR and upload releases")
    parser.add_argument(
        "--env", metavar="PATH", help="Load env vars from file (KEY=VALUE per line)"
    )
    parser.add_argument(
        "--project-root", default=".", help="Project directory containing release.toml"
    )
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
    validate_config(config, "aur")
    aur = config.section("aur")
    srcinfo_helper_image = aur.get("srcinfo_helper_image")
    required_tools = ["git", "ssh"]
    required_tools.append("docker" if srcinfo_helper_image else "makepkg")
    check_tools(*required_tools)
    version = project_version(config)
    hosting = config.section("hosting")
    aur_hosting = hosting.get("aur", {})
    aur_host = aur_hosting.get("host", "aur.archlinux.org")
    aur_user = aur_hosting.get("ssh_user", "aur")
    maintainer = aur_hosting.get("maintainer", config.project.get("maintainer", ""))
    if not maintainer:
        print("Error: an AUR maintainer identity is required", file=sys.stderr)
        return 1

    if args.type in ("git", "both"):
        release_git(
            config,
            version,
            aur_host=aur_host,
            aur_user=aur_user,
            maintainer=maintainer,
            srcinfo_helper_image=srcinfo_helper_image,
        )

    if args.type in ("bin", "both"):
        public_base_url = hosting.get("public_base_url", "")
        if not isinstance(public_base_url, str) or not public_base_url.startswith(
            "https://"
        ):
            print(
                "Error: [hosting].public_base_url must be an https:// URL "
                "for binary AUR publishing",
                file=sys.stderr,
            )
            return 1
        archives = release_archives(config, version)
        print("Uploading release archives to R2 ...")
        upload_releases(config, archives)
        release_bin(
            config,
            version,
            archives,
            aur_host=aur_host,
            aur_user=aur_user,
            maintainer=maintainer,
            srcinfo_helper_image=srcinfo_helper_image,
        )


if __name__ == "__main__":
    sys.exit(main())
