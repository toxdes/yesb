#!/usr/bin/env python3
"""Create release packages for the project mounted at /build."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import tomllib

ROOT = Path(os.environ.get("PACKAGE_ROOT", "/build"))
OUT = Path(os.environ.get("OUTPUT_DIR", "/output"))
ARCH = os.environ.get("TARGETARCH", "amd64")
INCLUDE_APPIMAGE = os.environ.get("INCLUDE_APPIMAGE", "") == "1"

with (ROOT / "release.toml").open("rb") as file:
    CONFIG = tomllib.load(file)

PROJECT = CONFIG["project"]
PROJECT_ID = PROJECT["id"]
VERSION = os.environ.get("VERSION")

PACKAGE = CONFIG.get("package", {})
BINARY = PACKAGE.get("binary", PROJECT_ID)
ARCH_MAP = {
    "amd64": {"deb": "amd64", "rpm": "x86_64"},
    "arm64": {"deb": "arm64", "rpm": "aarch64"},
}
if ARCH not in ARCH_MAP:
    raise SystemExit(f"Unknown arch: {ARCH}")

DEB_ARCH = ARCH_MAP[ARCH]["deb"]
RPM_ARCH = ARCH_MAP[ARCH]["rpm"]


def config_error(message):
    raise SystemExit(f"Invalid release.toml: {message}")


def root_path(value, label, *, must_exist=True):
    path = (ROOT / value).resolve()
    root = ROOT.resolve()
    if not path.is_relative_to(root) or path == root:
        config_error(f"{label} must stay inside {ROOT}")
    if must_exist and not path.is_file():
        config_error(f"{label} does not point to a file: {path}")
    return path


for key in ("id", "description", "maintainer", "license", "version_file"):
    if not isinstance(PROJECT.get(key), str) or not PROJECT[key].strip():
        config_error(f"[project].{key} must be a non-empty string")
if "deb" not in CONFIG or not isinstance(CONFIG["deb"], dict):
    config_error("missing [deb] section")
if "rpm" not in CONFIG or not isinstance(CONFIG["rpm"], dict):
    config_error("missing [rpm] section")
if Path(BINARY).name != BINARY:
    config_error("[package].binary must be a filename located directly under /build")
root_path(BINARY, "[package].binary")
if not VERSION:
    VERSION = (
        root_path(PROJECT["version_file"], "[project].version_file").read_text().strip()
    )
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._~]*", VERSION):
    config_error("project version contains unsupported characters")


def assets_for(kind):
    return PACKAGE.get(kind, {}).get("assets", [])


def install_assets(root, assets):
    for asset in assets:
        source = root_path(asset["source"], "asset source")
        configured_destination = Path(str(asset["dest"]))
        if (
            not configured_destination.is_absolute()
            or ".." in configured_destination.parts
        ):
            config_error("asset dest must be an absolute package path")
        destination = root / str(configured_destination).lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(int(asset.get("mode", 0o644)))


def build_deb():
    package = CONFIG["deb"]
    name = f"{package.get('package_name', PROJECT_ID)}_{VERSION}_{DEB_ARCH}"
    pkg = Path("/pkg-deb")
    (pkg / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (pkg / "usr/bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root_path(BINARY, "[package].binary"), pkg / "usr/bin" / BINARY)
    (pkg / "usr/bin" / BINARY).chmod(0o755)
    install_assets(pkg, assets_for("deb"))

    depends = ", ".join(package.get("depends", []))
    control = "\n".join(
        [
            f"Package: {package.get('package_name', PROJECT_ID)}",
            f"Version: {VERSION}",
            f"Architecture: {DEB_ARCH}",
            f"Maintainer: {PROJECT['maintainer']}",
            f"Section: {package.get('section', 'utils')}",
            f"Priority: {package.get('priority', 'optional')}",
            f"Depends: {depends}" if depends else "",
            f"Description: {PROJECT['description']}",
            f" {PROJECT['description']}",
            "",
        ]
    )
    (pkg / "DEBIAN/control").write_text(control)
    destination = OUT / f"{name}.deb"
    subprocess.run(["dpkg-deb", "--build", str(pkg), str(destination)], check=True)
    print(f"  -> {destination}")
    shutil.rmtree(pkg)


def build_rpm():
    package = CONFIG["rpm"]
    package_name = package.get("package_name", PROJECT_ID)
    name = f"{package_name}-{VERSION}-{package.get('release', '1')}.{RPM_ARCH}"
    topdir = Path("/tmp/rpm")
    for directory in ["BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"]:
        (topdir / directory).mkdir(parents=True, exist_ok=True)

    assets = assets_for("rpm")
    directories = {str(Path(asset["dest"]).parent) for asset in assets}
    mkdirs = " ".join(
        f"%{{buildroot}}/{directory.lstrip('/')}" for directory in sorted(directories)
    )
    installs = []
    files = []
    for asset in assets:
        destination = str(asset["dest"]).lstrip("/")
        mode = int(asset.get("mode", 0o644))
        installs.append(
            f"install -m{mode:o} /build/{asset['source']} %{{buildroot}}/{destination}"
        )
        files.append(f"/{destination}")

    requires = "\n".join(
        f"Requires:      {value}" for value in package.get("requires", [])
    )
    spec = f"""Name:           {package_name}
Version:        {VERSION}
Release:        {package.get("release", "1")}%{{?dist}}
Summary:        {PROJECT["description"]}
License:        {PROJECT["license"]}
BuildArch:      {RPM_ARCH}
{requires}

%description
{PROJECT["description"]}

%install
mkdir -p %{{buildroot}}/usr/bin {mkdirs}
install -m755 /build/{BINARY} %{{buildroot}}/usr/bin/{BINARY}
{chr(10).join(installs)}

%files
/usr/bin/{BINARY}
{chr(10).join(files)}
"""
    spec_path = topdir / "SPECS" / f"{package_name}.spec"
    spec_path.write_text(spec)
    subprocess.run(
        [
            "rpmbuild",
            "-bb",
            "--define",
            f"_topdir {topdir}",
            "--define",
            "dist %{nil}",
            str(spec_path),
        ],
        check=True,
    )

    source = topdir / "RPMS" / RPM_ARCH / f"{name}.rpm"
    destination = OUT / f"{name}.rpm"
    shutil.copy2(source, destination)
    print(f"  -> {destination}")
    shutil.rmtree(topdir)


def build_tar():
    name = f"{PROJECT_ID}_{VERSION}_{DEB_ARCH}"
    pkg = Path("/pkg-tar")
    (pkg / "usr/bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root_path(BINARY, "[package].binary"), pkg / "usr/bin" / BINARY)
    (pkg / "usr/bin" / BINARY).chmod(0o755)
    install_assets(pkg, assets_for("archive"))
    destination = OUT / f"{name}.tar.gz"
    subprocess.run(["tar", "-czf", str(destination), "-C", str(pkg), "."], check=True)
    print(f"  -> {destination}")
    shutil.rmtree(pkg)


def build_appimage():
    appimage = PACKAGE.get("appimage", {})
    runtime = Path("/usr/local/share/appimage-runtime")
    if not runtime.exists():
        raise SystemExit(
            "INCLUDE_APPIMAGE was requested but "
            "/usr/local/share/appimage-runtime is missing"
        )

    appdir = Path("/tmp/appdir")
    (appdir / "usr/bin").mkdir(parents=True, exist_ok=True)
    (appdir / "usr/lib").mkdir(parents=True, exist_ok=True)
    (appdir / "usr/share/glib-2.0/schemas").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root_path(BINARY, "[package].binary"), appdir / "usr/bin" / BINARY)
    (appdir / "usr/bin" / BINARY).chmod(0o755)

    icon = appimage.get("icon")
    desktop = appimage.get("desktop")
    if icon:
        icon_path = root_path(icon, "[package.appimage].icon")
        shutil.copy2(icon_path, appdir / Path(icon).name)
        shutil.copy2(icon_path, appdir / ".DirIcon")
    if desktop:
        desktop_path = root_path(desktop, "[package.appimage].desktop")
        shutil.copy2(desktop_path, appdir / Path(desktop).name)

    skip = (
        "ld-linux",
        "libc.so",
        "libm.so",
        "libpthread",
        "libdl.so",
        "libstdc++.so",
        "libgcc_s.so",
        "libresolv.so",
        "librt.so",
        "libutil.so",
        "libnss_",
        "libnsl",
    )
    result = subprocess.run(
        ["ldd", str(root_path(BINARY, "[package].binary"))],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if "=>" not in parts:
            continue
        path = Path(parts[parts.index("=>") + 1])
        if path.is_absolute() and path.exists() and not path.name.startswith(skip):
            shutil.copy2(path, appdir / "usr/lib")

    schemas = Path("/usr/share/glib-2.0/schemas")
    if schemas.exists():
        for schema in schemas.glob("org.gtk*"):
            shutil.copy2(schema, appdir / "usr/share/glib-2.0/schemas")
        compiled = schemas / "gschemas.compiled"
        if compiled.exists():
            shutil.copy2(compiled, appdir / "usr/share/glib-2.0/schemas")
    subprocess.run(
        ["glib-compile-schemas", str(appdir / "usr/share/glib-2.0/schemas")],
        check=False,
    )

    apprun = f"""#!/bin/bash
APPDIR="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="${{APPDIR}}/usr/lib:${{LD_LIBRARY_PATH}}"
export GSETTINGS_SCHEMA_DIR="${{APPDIR}}/usr/share/glib-2.0/schemas"
if [ -n "$WAYLAND_DISPLAY" ]; then
    export GDK_BACKEND=wayland
else
    export GDK_BACKEND=x11
fi
exec "${{APPDIR}}/usr/bin/{BINARY}" "$@"
"""
    (appdir / "AppRun").write_text(apprun)
    (appdir / "AppRun").chmod(0o755)

    squashed = Path(f"/tmp/{PROJECT_ID}.squashfs")
    subprocess.run(["mksquashfs", str(appdir), str(squashed), "-noappend"], check=True)
    destination = OUT / f"{PROJECT_ID}-{VERSION}-{ARCH}.AppImage"
    with destination.open("wb") as output:
        output.write(runtime.read_bytes())
        output.write(squashed.read_bytes())
    destination.chmod(0o755)
    print(f"  -> {destination}")
    shutil.rmtree(appdir)
    squashed.unlink()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    build_deb()
    build_rpm()
    build_tar()
    if INCLUDE_APPIMAGE:
        print("Including AppImage...")
        build_appimage()
    print("Done.")


if __name__ == "__main__":
    main()
