# `release.toml` reference

Paths are relative to the directory containing `release.toml` and must remain
inside that project directory. Package asset destinations are absolute paths
inside the package and must not contain `..`.

## Project

`[project]` requires `id` and `version_file`.

```toml
[project]
id = "myapp"
name = "My App"
description = "A short package description"
version_file = "VERSION"
maintainer = "Your Name <you@example.com>"
homepage = "https://example.com/myapp"
repository = "https://github.com/example/myapp"
license = "MIT"
```

Package creation also expects `description`, `maintainer`, and `license` when
the corresponding DEB or RPM output is built. AUR metadata uses `homepage`,
falling back to `repository`.

## Build

```toml
[build]
dockerfile = "Dockerfile"       # default
context = "."                   # default
output_dir = "dist"             # default
platforms = ["linux/amd64"]     # default

[build.args]
EXAMPLE = "value"
```

Yesb always supplies default `VERSION` and `GIT_SHA` build arguments unless
they are explicitly set in `[build.args]`. It supplies `INCLUDE_APPIMAGE=1`
when requested on the command line.

## Release artifacts

Release artifacts are opaque files that can be served directly, regardless of
their operating system or file type:

```toml
[[release.artifacts]]
source = "artifacts/myapp-windows.zip"       # optional external input
name = "myapp-{version}-windows.zip"
platform = "windows"                         # optional metadata
architecture = "x86_64"                      # optional metadata

[[release.artifacts]]
name = "myapp_{version}_amd64.tar.gz"        # emitted by the build

[[release.artifacts]]
name = "myapp-{version}-amd64.AppImage"      # emitted by --include-appimage
```

`name` is the final filename and may contain the `{version}` placeholder.
When `source` is set, `build_all.py` copies that project-relative file into the
build output. When it is omitted, the project can emit the named file through a
separate build step before running `release_direct.py`. `build_all.py` leaves
such source-less artifacts independent of its Docker output. `platform` and
`architecture` are optional descriptive metadata. `release_direct.py` uploads
every configured artifact to the `[hosting].release_prefix` prefix and also
publishes a matching `.sha256` file.

## Package contents

```toml
[package]
binary = "myapp"                # defaults to project.id

[[package.deb.assets]]
source = "packaging/myapp.desktop"
dest = "/usr/share/applications/myapp.desktop"
mode = 0o644

[[package.rpm.assets]]
source = "packaging/myapp.desktop"
dest = "/usr/share/applications/myapp.desktop"
mode = 0o644

[[package.archive.assets]]
source = "LICENSE"
dest = "/usr/share/licenses/myapp/LICENSE"
mode = 0o644

[package.appimage]
icon = "packaging/myapp.png"
desktop = "packaging/myapp.desktop"
```

Each asset entry requires `source` and `dest`; `mode` defaults to `0o644`.

## Debian and RPM

```toml
[deb]
package_name = "myapp"          # defaults to project.id
architectures = ["amd64"]       # default for repository publishing
section = "utils"               # default
priority = "optional"           # default
depends = ["libexample1"]

[rpm]
package_name = "myapp"          # defaults to project.id
release = "1"                   # default
requires = ["example-libs"]
```

The supported build platforms are `linux/amd64` and `linux/arm64`; use
`deb.architectures = ["amd64", "arm64"]` in the same order when both are
requested. RPM artifacts are named
`package-version-release.x86_64.rpm` or `package-version-release.aarch64.rpm`.

## Hosting and AUR

```toml
[hosting]
public_base_url = "https://packages.example.com"
release_prefix = "releases"     # default
apt_prefix = "apt"              # default
rpm_prefix = "rpm"              # default

[hosting.apt]
suite = "stable"                # default
component = "main"              # default
origin = "myapp"                # defaults to project.id
label = "My App"                # defaults to project.name or project.id

[hosting.aur]
host = "aur.archlinux.org"       # default
ssh_user = "aur"                # default
maintainer = "Your Name <you@example.com>"

[aur]
git_pkgbuild = "packaging/PKGBUILD"
git_package = "myapp-git"        # defaults to <project.id>-git
binary_package = "myapp-bin"     # defaults to <project.id>-bin
depends = ["example-lib"]
```

`public_base_url` is required for usable generated binary AUR download URLs.
Binary AUR publishing expects both amd64 and arm64 release archives.

## Homebrew

Homebrew publishing is optional and updates a project-owned tap:

```toml
[homebrew]
tap = "yourorg/tap"
remote = "git@github.com:yourorg/homebrew-tap.git"  # optional GitHub default
branch = "main"                                      # default
formula = "myapp"
binary = "myapp"                                     # defaults to package.binary

[[homebrew.archives]]
artifact = "myapp-{version}-macos-x86_64.zip"
architecture = "x86_64"
url = "https://packages.example.com/releases/myapp-{version}-macos-x86_64.zip"

[[homebrew.archives]]
artifact = "myapp-{version}-macos-arm64.zip"
architecture = "arm64"
url = "https://packages.example.com/releases/myapp-{version}-macos-arm64.zip"
```

Archives must name entries from `[[release.artifacts]]`. Provide both Intel and
Apple Silicon archives, or one `universal` archive. `release_homebrew.py`
calculates checksums locally, writes `Formula/myapp.rb`, and pushes the tap; it
does not upload files or require R2 credentials.
