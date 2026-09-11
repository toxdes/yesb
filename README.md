# yesb

Yesb is a small build and release toolkit for Linux applications. It gives
independent projects one repeatable path from a Docker build to Debian, RPM,
archive, and optional AppImage artifacts, then publishes package repositories
and AUR packages when needed.

The intended setup is to add Yesb as a submodule. The application keeps its
own `release.toml`, version file, and Dockerfile, so release policy stays with
the project being released.

## Setup

```sh
git submodule add https://github.com/toxdes/yesb.git yesb
```

Create `release.toml` in the application root:

```toml
[project]
id = "myapp"
name = "My App"
description = "A short package description"
version_file = "VERSION"
maintainer = "Your Name <you@example.com>"
homepage = "https://example.com/myapp"
license = "MIT"

[build]
dockerfile = "Dockerfile"
context = "."
output_dir = "dist"
platforms = ["linux/amd64", "linux/arm64"]

[package]
binary = "myapp"

[deb]
package_name = "myapp"
architectures = ["amd64", "arm64"]

[rpm]
package_name = "myapp"

[aur]
# srcinfo_helper_image = "ghcr.io/toxdes/yesb-aur-helper@sha256:<64 lowercase hex digits>"

[hosting]
public_base_url = "https://packages.example.com"
```

The Dockerfile must use `TARGETARCH` and accept the `VERSION` and `GIT_SHA`
build arguments. A packaging stage can call `yesb/package.py` to write `.deb`,
`.rpm`, and `.tar.gz` files to `/output`; the final stage must export the
contents of that directory at its filesystem root.

Build from the application root:

```sh
./yesb/build_all.py
```

Artifacts and SHA-256 checksum files are written to `dist/` by default. Add
`--include-appimage` when the Dockerfile provides the required AppImage tools
and runtime.

To inspect repositories without publishing them:

```sh
./yesb/release_apt.py --dry-run
./yesb/release_rpm.py --dry-run
```

Pass `--project-root PATH` when running a script outside the application root.
The publishing scripts also accept `--env PATH` for `KEY=VALUE` files.

### AUR metadata helper

By default, `release_aur.py` requires a local `makepkg`. Projects publishing
from Ubuntu, Debian, or another non-Arch host can set
`[aur].srcinfo_helper_image` to a Docker/OCI image pinned by a full SHA-256
digest. Yesb then runs only `makepkg --printsrcinfo` in that image; cloning,
Git commits, AUR pushes, and R2 uploads remain on the host.

The helper runs without network access, with a read-only checkout mount, a
writable in-container tmpfs, dropped capabilities, and a non-root UID. Do not
mount SSH keys, agent sockets, cloud credentials, or the Docker socket into
the helper. `aur-helper.Dockerfile` provides a minimal image recipe; build it
from a digest-pinned `archlinux:base-devel` image and record the pushed image's
full digest in each project's `release.toml`.

APT and RPM publication keeps only the latest package version for the project
being released. Shared packages from other projects remain available, while
existing immutable package objects are skipped instead of reuploaded. Indexes
and signatures are regenerated for every release. Publishing a version whose
package object already exists is rejected, and a permanent publication marker
prevents republishing a retired version; bump the version to publish again.

## Requirements

- Python 3.11 or newer
- Docker with Buildx configured for every requested platform; install
  QEMU/binfmt explicitly when the builder needs cross-platform emulation
- `uv` for publisher script dependencies
- APT publishing: `dpkg`, `dpkg-deb`, and `gpg`
- RPM publishing: `createrepo_c`, `rpmsign`, and `gpg`
- AUR publishing: `git`, `ssh`, and an authenticated AUR account; either local
  `makepkg` or Docker when `[aur].srcinfo_helper_image` is configured
- Package container: `dpkg-deb`, `rpmbuild`, and `tar`; AppImage builds also
  need `ldd`, `glib-compile-schemas`, `mksquashfs`, and an AppImage runtime

## Environment

R2 uploads require:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_ENDPOINT_URL
AWS_BUCKET
```

Set `GPG_KEY_ID` to sign APT and RPM repositories. Set `GPG_PASSPHRASE` only
when that key requires one. Production repository uploads require signing
unless `--allow-unsigned` is passed explicitly. `R2_REGION` is optional and
defaults to `auto`.

`cf_purge_cache.py` requires `CF_API_TOKEN` and `CF_ZONE_ID`. Its `--set-ttl`
mode also requires `CF_BROWSER_TTL` and `CF_EDGE_TTL`.

## Agent skill

Install the Yesb skill for supported coding agents with:

```sh
npx skills add toxdes/yesb
```

## License

MIT
