# syntax=docker/dockerfile:1
ARG FEDORA_BASE=fedora:44@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80
FROM ${FEDORA_BASE}
RUN dnf install -y python3 rpm-build rpm-sign createrepo_c && dnf clean all
WORKDIR /opt/yesb
COPY helper_runner.py helper_protocol.py rpm_sign_adapter.py release_apt.py release_rpm.py release_lib.py container_tools.py signing.py rpm_integrity.py ./
ENTRYPOINT ["python3", "/opt/yesb/helper_runner.py"]
