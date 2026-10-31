# syntax=docker/dockerfile:1
ARG DEBIAN_BASE=debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
FROM ${DEBIAN_BASE}
RUN apt-get update && apt-get install -y --no-install-recommends python3 dpkg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/yesb
COPY helper_runner.py helper_protocol.py rpm_sign_adapter.py release_apt.py release_rpm.py release_lib.py container_tools.py signing.py rpm_integrity.py ./
ENTRYPOINT ["python3", "/opt/yesb/helper_runner.py"]
