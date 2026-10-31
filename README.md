# yesb

Yesb is a small build and release toolkit for applications. It gives
independent projects one repeatable path from a Docker build to Debian, RPM,
archive, optional AppImage, and other release artifacts, then publishes package
repositories, direct downloads, optional Docker images, and AUR packages when
needed.

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

[docker]
image = "docker.io/yourorg/myapp"
dockerfile = "Dockerfile.runtime"
platforms = ["linux/amd64", "linux/arm64"]
publish_latest = false

[deb]
helper_image = "docker.io/yourorg/yesb-apt-helper@sha256:<64 lowercase hex digits>"
package_name = "myapp"
architectures = ["amd64", "arm64"]

[rpm]
helper_image = "docker.io/yourorg/yesb-rpm-helper@sha256:<64 lowercase hex digits>"
package_name = "myapp"

[release]
[[release.artifacts]]
source = "artifacts/myapp-windows.zip"
name = "myapp-{version}-windows.zip"
platform = "windows"                    # optional metadata
architecture = "x86_64"                 # optional metadata

[aur]
srcinfo_helper_image = "docker.io/toxdes/yesb-aur-helper@sha256:<64 lowercase hex digits>"

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
and runtime. Projects may declare externally-built files in
`[[release.artifacts]]`; those files are copied into `dist/` after the Docker
build when a `source` is configured. Source-less artifacts are useful when a
project's own build step emits files into `dist/`; `build_all.py` leaves those
independent artifacts alone, while `release_direct.py` requires every declared
artifact to be present before uploading. Optional `platform` and
`architecture` fields are descriptive metadata and do not restrict filenames.

To publish declared artifacts as direct downloads:

```sh
./yesb/release_direct.py --env PATH
```

The files are uploaded under the configured `release_prefix` without regard to
their operating system or file type. Existing versioned objects are skipped.

### Docker image

Docker image publishing is optional and independent of the package and archive
release flows. The project owns the runtime Dockerfile; yesb only builds it
with Buildx and pushes the configured version tag. Docker credentials must be
configured separately with `docker login` or a credential helper.

```toml
[docker]
image = "docker.io/yourorg/myapp"
dockerfile = "Dockerfile.runtime"
context = "."
platforms = ["linux/amd64", "linux/arm64"]
publish_latest = false

[docker.args]
EXAMPLE = "value"
```

The image is published as `yourorg/myapp:<version>`. Set
`publish_latest = true` to also move the `latest` tag to this release. Use
`target` when the runtime image is a named stage in a shared Dockerfile.

```sh
./yesb/release_docker.py
```

Use `--dry-run` to inspect the Buildx command without publishing. Projects
without `[docker]` skip this step.

### Homebrew tap

Homebrew support is optional and independent of direct artifact publishing.
Configure a self-maintained tap and reference macOS CLI archives from the
generic release artifact list:

```toml
[homebrew]
tap = "yourorg/tap"
remote = "git@github.com:yourorg/homebrew-tap.git"
branch = "main"
formula = "myapp"
binary = "myapp"

[[homebrew.assets]]
source = "share/man/man1/myapp.1"
destination = "man1"

[[homebrew.archives]]
artifact = "myapp-{version}-macos-x86_64.zip"
architecture = "x86_64"
url = "https://packages.example.com/releases/myapp-{version}-macos-x86_64.zip"

[[homebrew.archives]]
artifact = "myapp-{version}-macos-arm64.zip"
architecture = "arm64"
url = "https://packages.example.com/releases/myapp-{version}-macos-arm64.zip"
```

Publish the formula with:

```sh
./yesb/release_homebrew.py
```

Optional `[[homebrew.assets]]` entries install additional files from the archive
using standard Homebrew installation methods such as `man1`, `share`, or
`include`. This updates and pushes the tap repository only; it does not upload
artifacts.
Users can install the formula with `brew install yourorg/tap/myapp`.

To inspect repositories without publishing them:

```sh
./yesb/release_apt.py --dry-run
./yesb/release_rpm.py --dry-run
```

Pass `--project-root PATH` when running a script outside the application root.
The publishing scripts also accept `--env PATH` for `KEY=VALUE` files.

### AUR package alternatives

Use the unsuffixed package for source builds of a tagged stable release:

```sh
yay -S myapp
```

Offer `myapp-bin` for the prebuilt release and `myapp-git` for the latest
development source. Keep these distinct, following the
[AUR submission guidelines](https://wiki.archlinux.org/title/AUR_submission_guidelines).
`provides` satisfies dependencies; it does not redirect explicit install commands.

Set `[aur].source_pkgbuild` to a project-owned stable recipe. It must pin the
release tag (for example `#tag=v${pkgver}`), omit a moving `pkgver()` function,
and use the canonical `[aur].package_name` (default: project ID). Yesb updates
the static `pkgver` for each release without rewriting the recipe's build steps.
Publish it with `--type source`; `--type all` (the default) publishes all
configured source variants and the binary package. Existing `--type both`
continues to mean `git` plus `bin`. Source-only publishing needs no R2 credentials
or built binary archives. Projects without `source_pkgbuild` skip that variant.

Generated binary recipes declare `provides=("myapp=${pkgver}")` and
`conflicts=('myapp')`, using `[project].id` as the base name. Set
`[aur].package_name` when the canonical package name differs from that ID.
The base package does not need to provide or conflict with itself.

Project-owned `-git` PKGBUILDs must declare the same relationships, using
double quotes around `myapp=${pkgver}` so makepkg expands the version. Yesb
checks the evaluated `.SRCINFO` before committing or pushing an AUR recipe;
missing or stale relationships fail with an instruction to update the source
PKGBUILD. It does not rewrite project-owned declarations.

Optional `[aur].provides` and `[aur].conflicts` arrays replace the defaults
for both variants. `{version}` expands to the published version; empty arrays
disable the respective requirement for intentional exceptions. For example:

```toml
[aur]
package_name = "myapp"
provides = ["myapp={version}", "other-interface"]
conflicts = ["myapp", "old-app"]
```

### APT and RPM helpers

Configure `[deb].helper_image` and `[rpm].helper_image` with published image
references pinned by SHA-256 digest. APT runs only its Debian helper; RPM runs
only its Fedora helper. Builds, direct uploads, Homebrew and Docker-image
publication do not require these settings. Existing manifests must add the
corresponding image reference before using these publishers; host-tool fallback
is not used. No helper images are published automatically.

Build the runtime images locally from the Yesb directory:

```sh
docker buildx build --load -f apt-helper.Dockerfile -t yesb-apt-helper:local .
docker buildx build --load -f rpm-helper.Dockerfile -t yesb-rpm-helper:local .
```

The Dockerfiles pin their official Debian/Fedora bases. They include only runtime
code and tools; tests and project files are excluded. Maintainers publish AMD64
and ARM64 variants under one multi-platform image, then consumers record the
manifest digest. Updating a digest is an explicit tooling upgrade. Projects may
optionally use these images for their own packaging commands with an overridden
entrypoint; `build_all.py` and the project's Dockerfile contract are unchanged.

Helpers run on the Docker host's native architecture, not the package target's.
An AMD64 helper can prepare indexes for ARM64 packages. Each invocation mounts
only staged packages/metadata read-only and a temporary output directory writable.
It uses the caller's UID/GID, a read-only root filesystem, dropped capabilities,
no network, no agent mounts, and no publishing credentials. The Docker daemon
must have access to these local staging paths. Linux has been integration-tested;
Docker Desktop hosts require validation of file sharing and host GPG/pinentry.

`GPG_KEY_ID` selects an existing host private key. `GPG_PASSPHRASE`, when supplied,
is sent to host GPG through stdin; otherwise normal agent/pinentry behavior applies.
APT's Release and RPM's repomd.xml are signed and verified on the host. RPM package
signing runs `rpmsign` inside Fedora: its signing adapter relays bounded requests
over a versioned stdin/stdout protocol to host GPG. An internal Unix socket joins
the adapter to its container runner; no host socket or network service is exposed.
Only the invocation's selected key can be used, with at most four detached
signature requests per package and a 16 MiB limit on each request. Unknown protocol
versions, missing output, failed signing, invalid package signatures, and helper
timeouts stop publication. Cancellation stops the helper and discards staging.

The host authorizes only the exact headers read from the selected RPM inputs.
It independently checks that returned RPM headers and payloads are unchanged and
verifies package signatures with the selected host key. The envelope reader
accepts immutable RPM v4/v6 headers; unsupported layouts fail closed. APT payload
bytes must match their inputs. Repository output is restricted to the selected
packages and expected metadata, with checksum and final upload-manifest checks.
Repository paths must be portable relative paths; APT suite and component each
use one path component. Protocol reads and writes share a ten-minute deadline.
Pinned images remain trusted build tools, not a replacement for reviewing package
contents and repository metadata.

### AUR metadata helper

`release_aur.py` requires `[aur].srcinfo_helper_image`, a Docker/OCI image
pinned by a full SHA-256 digest. Yesb then runs only `makepkg --printsrcinfo` in that image; cloning,
Git commits, AUR pushes, and R2 uploads remain on the host.

The helper runs without network access, with a read-only checkout mount, a
writable in-container tmpfs, dropped capabilities, and a non-root UID. Do not
mount SSH keys, agent sockets, cloud credentials, or the Docker socket into
the helper. `aur-helper.Dockerfile` provides a minimal image recipe; build it
from a digest-pinned `archlinux:base-devel` image and record the pushed image's
full digest in each project's `release.toml`.

APT and RPM publication keeps only the latest package version for the project
being released. Shared packages from other projects remain available, while
existing immutable package objects are skipped instead of reuploaded. RPM
updates download only `repomd.xml` and its primary, filelists and other XML
indexes. `createrepo_c` reuses their package metadata with an explicit package
list and `--skip-stat`; unchanged RPM payloads remain in R2. Missing, corrupt
or incomplete indexes fail before upload, rather than dropping shared packages.
Indexes and signatures are regenerated for every release. Publishing a version whose
package object already exists is rejected, and a permanent publication marker
prevents republishing a retired version; bump the version to publish again.
Versioned release artifacts are immutable; repository indexes, signatures and
public keys may refresh, and repository lock objects coordinate publishers.

## Requirements

The host need not run Ubuntu or Debian. Builds run in the project's Dockerfile
through Buildx; its Linux base image and packaging tools are independent of the
host distribution. Publisher orchestration runs on the host. Distro-specific repository tools run
in the selected helper image; install no Debian, Fedora or Arch packaging tools
on the host.

- Python 3.11 or newer
- Docker with Buildx configured for every requested platform; install
  QEMU/binfmt explicitly when the builder needs cross-platform emulation
- `uv` for publisher script dependencies
- APT publishing: Docker and host `gpg` when signing
- RPM publishing: Docker and host `gpg` when signing; RPM tools run in Fedora
- AUR publishing: Docker, `git`, `ssh`, an authenticated AUR account, and host
  signing tools if required by Git configuration
- Homebrew publishing: `git` and credentials for the configured tap remote
- Docker publishing: Docker with Buildx and credentials for the configured
  registry
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
