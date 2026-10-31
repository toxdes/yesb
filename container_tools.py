"""Run credential-free repository helpers with host-controlled OpenPGP signing."""

import contextlib
import csv
import email.parser
import gzip
import hashlib
import io
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from helper_protocol import decode_data, encode_data, read_message, write_message
from release_lib import PINNED_IMAGE, SAFE_IDENTIFIER, confined_path, safe_object_path
from rpm_integrity import RpmSigningPolicy

HELPER_TIMEOUT = 600


def bind_mount(source, target, readonly=False):
    fields = ["type=bind", f"src={source}", f"dst={target}"]
    if readonly:
        fields.append("readonly")
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(fields)
    return output.getvalue()


def host_identity():
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    return uid, gid


def regular_tree(root):
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RuntimeError(f"Helper output contains a nonregular entry: {path.relative_to(root)}")


def _read_messages(stream, messages, stopped):
    def send(value):
        while not stopped.is_set():
            try:
                messages.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    try:
        while not stopped.is_set():
            message = read_message(stream)
            send(message)
            if message.get("kind") == "complete":
                return
    except Exception as error:
        send(error)


def _write_messages(stream, outgoing, acknowledgements, stopped):
    while not stopped.is_set():
        try:
            kind, fields = outgoing.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            write_message(stream, kind, **fields)
            acknowledgements.put(None)
        except Exception as error:
            acknowledgements.put(error)
            return


def exchange(command, request, signer=None, policy=None):
    """Authorize RPM headers and bound both protocol directions by one deadline."""
    messages = queue.Queue(maxsize=2)
    stopped = threading.Event()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    reader = threading.Thread(target=_read_messages, args=(process.stdout, messages, stopped), daemon=True)
    outgoing, acknowledgements = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
    writer = threading.Thread(target=_write_messages, args=(process.stdin, outgoing, acknowledgements, stopped), daemon=True)
    reader.start()
    writer.start()
    deadline = time.monotonic() + HELPER_TIMEOUT

    def remaining():
        duration = deadline - time.monotonic()
        if duration <= 0:
            raise RuntimeError("Repository helper timed out")
        return duration

    def send(kind, **fields):
        try:
            outgoing.put((kind, fields), timeout=remaining())
            error = acknowledgements.get(timeout=remaining())
        except queue.Full as error:
            raise RuntimeError("Repository helper timed out writing a message") from error
        except queue.Empty as error:
            raise RuntimeError("Repository helper timed out writing a message") from error
        if error is not None:
            raise RuntimeError(f"Repository helper protocol write failed: {error}") from error

    try:
        send("prepare", **request)
        signatures = 0
        while True:
            try:
                response = messages.get(timeout=remaining())
            except queue.Empty as error:
                raise RuntimeError("Repository helper timed out") from error
            if isinstance(response, Exception):
                raise RuntimeError(f"Repository helper protocol failed: {response}") from response
            kind = response.get("kind")
            if kind == "complete":
                if process.wait(timeout=min(30, remaining())) != 0:
                    raise RuntimeError("Repository helper failed")
                return
            if kind != "sign" or signer is None or request["operation"] != "rpm":
                raise RuntimeError("Unexpected repository helper signing request")
            signatures += 1
            if signatures > 4 * len(request["packages"]):
                raise RuntimeError("Repository helper exceeded the signature request limit")
            data = decode_data(response.get("data"))
            if policy is None:
                raise RuntimeError("RPM signing requires an approved package policy")
            policy.authorize(data)
            signature = signer.sign(data)
            send("signature", data=encode_data(signature))
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        writer.join(timeout=5)
        reader.join(timeout=5)
        with contextlib.suppress(OSError):
            process.stdin.close()
        process.stdout.close()


def prepare_repository(operation, image, destination, packages, options, *, existing_metadata=None, signer=None):
    if operation not in {"apt", "rpm"}:
        raise RuntimeError("Unsupported repository operation")
    if not PINNED_IMAGE.fullmatch(image) and not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise RuntimeError("Repository helper image must be pinned by sha256")
    validate_options(operation, options)
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Repository staging directory must be empty")
    with tempfile.TemporaryDirectory(prefix=f"yesb-{operation}-helper-") as directory:
        root = Path(directory)
        inputs, outputs = root / "input", root / "output"
        inputs.mkdir()
        outputs.mkdir()
        package_dir = inputs / "packages"
        package_dir.mkdir()
        names = []
        for package in packages:
            if package.name in names:
                raise RuntimeError("Duplicate helper package filename")
            names.append(package.name)
            shutil.copyfile(package, package_dir / package.name)
        policy = RpmSigningPolicy(list(package_dir.iterdir())) if operation == "rpm" else None
        options = dict(options)
        if existing_metadata is not None:
            regular_tree(existing_metadata)
            shutil.copytree(existing_metadata, inputs / "existing")
            options["existing_metadata"] = "existing"
        request = {
            "operation": operation, "packages": [f"packages/{name}" for name in names],
            "options": options,
        }
        if signer is not None:
            request.update(key=signer.fingerprint, public_key=signer.public_key)
        container = f"yesb-{operation}-{uuid.uuid4().hex}"
        uid, gid = host_identity()
        command = [
            "docker", "run", "--rm", "--init", "--interactive", "--name", container,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user", f"{uid}:{gid}",
            "--tmpfs", "/tmp:rw,nosuid,size=256m,mode=1777",
            "--mount", bind_mount(inputs, "/input", True),
            "--mount", bind_mount(outputs, "/output"),
            "--env", "HOME=/tmp", "--env", "LC_ALL=C", image,
        ]
        try:
            exchange(command, request, signer, policy)
            manifest = validate_output(operation, outputs, names, options)
            if policy is not None:
                for name, envelope in policy.packages.items():
                    envelope.verify_output(confined_path(outputs, f"{options['prefix']}/{name}"), signer)
            shutil.copytree(outputs, destination, dirs_exist_ok=True)
            return manifest
        finally:
            # stop only this invocation's container, including after Ctrl+C
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=15)


def validate_options(operation, options):
    safe_object_path(options["prefix"])
    for key in ("package_name", "suite", "component"):
        if key in options and (not isinstance(options[key], str) or not SAFE_IDENTIFIER.fullmatch(options[key])):
            raise RuntimeError(f"Repository option {key} must be a safe path component")
        if key in options:
            safe_object_path(options[key])
    if operation == "apt":
        if not options.get("archs") or any(arch not in {"amd64", "arm64"} for arch in options["archs"]):
            raise RuntimeError("Unsupported APT architectures")
        for key in ("origin", "label"):
            value = options[key]
            if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise RuntimeError(f"Repository option {key} must be a single line")


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def validate_apt_metadata(prefix, expected, options):
    from release_apt import merge_packages, parse_packages

    dist = prefix / "dists" / options["suite"]
    release = email.parser.Parser().parsestr((dist / "Release").read_text())
    for field, value in {"Suite": options["suite"], "Codename": options["suite"],
                         "Components": options["component"], "Architectures": " ".join(options["archs"]),
                         "Origin": options["origin"], "Label": options["label"]}.items():
        if release.get_all(field) != [value]:
            raise RuntimeError(f"Unexpected APT Release field: {field}")
    indexes = set()
    for arch in options["archs"]:
        index = dist / options["component"] / f"binary-{arch}" / "Packages"
        content = index.read_text()
        with gzip.open(index.with_name("Packages.gz"), "rt") as stream:
            if stream.read() != content:
                raise RuntimeError("APT compressed index differs from Packages")
        current = {}
        for stanza in parse_packages(content).values():
            fields = email.parser.Parser().parsestr(stanza)
            if fields["Package"] == options["package_name"]:
                name = fields.get("Filename", "")
                path = confined_path(prefix, name)
                if path not in expected or fields.get("Architecture") != arch or name in current:
                    raise RuntimeError("Unexpected APT package index entry")
                if fields.get("SHA256") != digest(path) or fields.get("Size") != str(path.stat().st_size):
                    raise RuntimeError("APT package index checksum mismatch")
                current[name] = stanza
        if set(current) != {path.relative_to(prefix).as_posix() for path in expected if path.name.endswith(f"_{arch}.deb")}:
            raise RuntimeError("APT package index is missing selected packages")
        old = options.get("existing_packages", {}).get(arch, "")
        merged = merge_packages(old, "\n\n".join(current.values()), options["package_name"])
        if parse_packages(merged) != parse_packages(content):
            raise RuntimeError("APT helper changed unrelated package records")
        indexes.update({index, index.with_name("Packages.gz")})
    hashes = release.get_all("SHA256")
    if hashes is None or len(hashes) != 1:
        raise RuntimeError("APT Release must contain one SHA256 manifest")
    actual = []
    for line in hashes[0].splitlines():
        if not line.strip():
            continue
        checksum, size, name = line.split()
        path = confined_path(dist, name)
        if path not in indexes or checksum != digest(path) or size != str(path.stat().st_size):
            raise RuntimeError("APT Release checksum mismatch")
        actual.append(path)
    if len(actual) != len(indexes) or set(actual) != indexes:
        raise RuntimeError("APT Release manifest does not match its indexes")
    return indexes


def validate_output(operation, outputs, names, options):
    regular_tree(outputs)
    validate_options(operation, options)
    prefix = confined_path(outputs, options["prefix"])
    if operation == "apt":
        release = prefix / "dists" / options["suite"] / "Release"
        expected = [prefix / "pool" / options["component"] / options["package_name"][0] / options["package_name"] / name for name in names]
    else:
        release = prefix / "repodata/repomd.xml"
        expected = [prefix / name for name in names]
    if not release.is_file() or not all(path.is_file() for path in expected):
        raise RuntimeError("Repository helper did not produce the required packages and metadata")
    allowed = {release, *expected}
    if operation == "apt":
        for path in expected:
            source = outputs.parent / "input/packages" / path.name
            if digest(source) != digest(path):
                raise RuntimeError("Repository helper modified a Debian package")
        allowed.update(validate_apt_metadata(prefix, expected, options))
    else:
        from release_rpm import RPMMD, metadata_records, retained_rpm_paths, validate_package_locations

        records = metadata_records(release)
        for kind, (href, entry) in records.items():
            path = confined_path(prefix, href)
            checksum = entry.find(f"{{{RPMMD}}}checksum")
            if checksum is None or checksum.get("type") != "sha256" or checksum.text != digest(path):
                raise RuntimeError("RPM repository metadata checksum mismatch")
            if path.name != f"{checksum.text}-{kind}.xml.gz":
                raise RuntimeError("Unexpected RPM metadata filename")
            allowed.add(path)
        retained = retained_rpm_paths(options["prefix"], options.get("existing_package_keys", ()), options["package_name"])
        validate_package_locations(prefix, [*retained, *names])
    parents = {parent for path in allowed for parent in path.parents if parent.is_relative_to(outputs)}
    for path in outputs.rglob("*"):
        if path not in (parents if path.is_dir() else allowed):
            raise RuntimeError(f"Unexpected helper output: {path.relative_to(outputs)}")
    return frozenset(path.relative_to(outputs).as_posix() for path in allowed)
