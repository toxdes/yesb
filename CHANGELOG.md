# Changelog

## Unreleased

### Changed

- APT and RPM publishers prepare repositories in pinned Debian and Fedora helpers.
- AUR publication requires its pinned Arch metadata helper instead of host makepkg.
- RPM package signing uses a per-run adapter to host GPG; host RPM tools are no longer required.
- Publishers require helper-image digests in their release.toml sections; project builds and artifacts are unchanged.

### Security

- Bind RPM signing requests to original package headers and verify returned package content and signatures on the host.
- Reject unexpected helper output and enforce publication manifests before accessing cloud credentials.
- Validate portable repository paths and APT metadata fields, with staging containment checks.
- Bound helper protocol reads and writes by one deadline, including blocked pipe writes.
