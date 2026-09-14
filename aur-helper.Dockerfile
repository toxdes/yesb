# syntax=docker/dockerfile:1
# Build with an explicitly digest-pinned Arch base, for example:
#   docker build --build-arg ARCH_BASE=archlinux:base-devel@sha256:<digest> \
#     -f aur-helper.Dockerfile -t docker.io/toxdes/yesb-aur-helper:latest .

ARG ARCH_BASE
FROM ${ARCH_BASE}

# Keep the runtime image limited to the Arch packaging tools. The caller runs
# makepkg as a non-root user with no network and no credentials mounted.
RUN pacman -Syu --noconfirm \
    && pacman -Scc --noconfirm

ENTRYPOINT []
