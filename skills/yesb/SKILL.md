---
name: yesb
description: Configure, build, validate, and publish Linux application releases with the toxdes/yesb toolkit. Use when a project needs a yesb submodule, release.toml, a compatible Dockerfile packaging stage, Debian or RPM repositories, AUR packages, release archives, checksums, Cloudflare R2 uploads, or Cloudflare cache operations.
---

# Yesb releases

Use Yesb as a submodule of the application being released. Keep application
metadata and build decisions in the application repository; do not customize
the Yesb scripts unless the user is changing the toolkit itself.

## Prepare a project

1. Inspect the project language, build commands, binary output, version source,
   assets, supported architectures, package dependencies, and existing
   Dockerfile before editing.
2. Check whether the Yesb submodule already exists. If absent and the user has
   asked for setup, add `https://github.com/toxdes/yesb.git` at `yesb/`. Do not
   replace another path or convert an existing checkout without permission.
3. Create or update the project-root `release.toml`. Read
   [release-toml.md](references/release-toml.md) for the supported schema.
4. Create or adapt the Dockerfile so its final stage contains only the files
   copied from the packaging stage's `/output` directory. Preserve the
   application's normal development or runtime targets where practical.
5. Make the build architecture-aware with Docker's `TARGETARCH`. Accept
   `VERSION` and `GIT_SHA`; use them in the application build when supported.
   Ensure the active Buildx builder already supports every requested platform;
   configure QEMU/binfmt explicitly when cross-platform emulation is needed.
6. Run a local build from the project root:

   ```sh
   ./yesb/build_all.py
   ```

   Add `--include-appimage` only when the container supplies the AppImage
   runtime and tools. Use `--project-root PATH` when invoked elsewhere.
7. Verify every configured architecture has the expected `.deb`, `.rpm`, and
   `.tar.gz`, plus matching `.sha256` entries and `SHA256SUMS`.
   RPM files must use `package-version-release.arch.rpm`; APT architectures
   and Docker platforms must be declared in matching order. Supported targets
   are currently `linux/amd64` and `linux/arm64`.

Never invent package dependencies. Derive them from the built executable and
the project's documented runtime requirements. Keep secrets out of
`release.toml`, Docker build arguments, tracked env files, and command output.

## Dockerfile contract

Use a multi-stage build. Build the application in an architecture-appropriate
stage, then use a Linux packaging stage that:

- copies `release.toml`, the version file, binary, and configured assets under
  `/build`;
- copies or mounts `yesb/package.py`;
- provides Python 3.11+, `dpkg-deb`, `rpmbuild`, and `tar`;
  - maps `ARG TARGETARCH`, `ARG VERSION`, `ARG GIT_SHA`, and
    `ARG INCLUDE_APPIMAGE` into the same-named environment variables before
    running `package.py`;
- ends with a scratch stage that copies the contents of `/output` to `/`.

For AppImage output, set `INCLUDE_APPIMAGE=1` (the default is disabled) and
provide `ldd`, `glib-compile-schemas`, `mksquashfs`, and
`/usr/local/share/appimage-runtime`. A requested AppImage fails the build when
the runtime is missing.

## Validate repositories

Build package repositories without publishing:

```sh
./yesb/release_apt.py --dry-run
./yesb/release_rpm.py --dry-run
```

Use `--serve` instead when the user wants to install-test a temporary local
repository. APT validation needs `dpkg`, `dpkg-deb`, and `gpg`; RPM validation
needs `createrepo_c`, `rpmsign`, and `gpg`. The publisher scripts use `uv` to
resolve their Python dependency. AUR publishing additionally needs `makepkg`,
`git`, and `ssh`.

## Publish

Treat publishing and cache purges as external state changes. Confirm that the
user intends the exact operation, verify artifacts and destination settings,
then run only the requested publisher:

```sh
./yesb/release_apt.py --env PATH
./yesb/release_rpm.py --env PATH
./yesb/release_aur.py --env PATH --type git|bin|both
./yesb/cf_purge_cache.py HOSTNAME --env PATH
```

R2 publishing requires `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINT_URL`, and `AWS_BUCKET`; `R2_REGION` defaults to `auto`. Signing is
enabled by `GPG_KEY_ID`, with optional `GPG_PASSPHRASE`. AUR publishing also
requires working SSH authentication to the configured AUR host. Production
APT and RPM uploads refuse unsigned publication unless the user explicitly
requests `--allow-unsigned`.

Cache operations require `CF_API_TOKEN` and `CF_ZONE_ID`. Setting TTLs also
requires `CF_BROWSER_TTL` and `CF_EDGE_TTL`.
