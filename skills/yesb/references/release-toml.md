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
