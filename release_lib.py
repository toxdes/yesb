"""Shared helpers for yesb release scripts."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import tomllib

SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")
SUPPORTED_DEB_ARCHITECTURES = ("amd64", "arm64")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._~]*$")
PINNED_IMAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*@sha256:[0-9a-f]{64}$"
)


@dataclass(frozen=True)
class ProjectConfig:
    """Loaded project manifest and the directory containing it."""

    data: dict
    path: Path

    @property
    def root(self):
        return self.path.parent

    @property
    def project(self):
        return self.data["project"]

    def section(self, name):
        return self.data.get(name, {})


def load_config(path=None):
    """Load and validate the common structure of a project's manifest."""
    config_path = Path(path or "release.toml").resolve()
    if not config_path.is_file():
        print(f"Error: {config_path} not found", file=sys.stderr)
        sys.exit(1)

    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        _config_error(str(error))

    project = data.get("project")
    if not isinstance(project, dict):
        print(f"Error: {config_path} is missing [project]", file=sys.stderr)
        sys.exit(1)

    missing = [name for name in ("id", "version_file") if name not in project]
    if missing:
        names = ", ".join(missing)
        print(f"Error: [project] is missing: {names}", file=sys.stderr)
        sys.exit(1)

    for name in ("build", "package", "deb", "rpm", "hosting", "aur"):
        section = data.get(name, {})
        if not isinstance(section, dict):
            _config_error(f"[{name}] must be a table")

    _require_string(project, "id", "[project]")
    _require_string(project, "version_file", "[project]")
    if not SAFE_IDENTIFIER.fullmatch(project["id"]):
        _config_error("[project].id contains unsupported characters")

    return ProjectConfig(data=data, path=config_path)


def _config_error(message):
    print(f"Error: invalid release.toml: {message}", file=sys.stderr)
    raise SystemExit(1)


def _require_string(section, key, section_name):
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        _config_error(f"{section_name}.{key} must be a non-empty string")
    return value


def project_path(config, value, label, *, allow_root=False, kind=None):
    """Resolve a manifest path while keeping it inside the project root."""
    if not isinstance(value, str) or not value.strip():
        _config_error(f"{label} must be a non-empty relative path")
    raw = Path(value)
    if raw.is_absolute():
        _config_error(f"{label} must be relative to the project root")

    root = config.root.resolve()
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root) or (resolved == root and not allow_root):
        _config_error(f"{label} must stay inside the project root")
    if kind == "file" and not resolved.is_file():
        _config_error(f"{label} does not point to a file: {resolved}")
    if kind == "directory" and not resolved.is_dir():
        _config_error(f"{label} does not point to a directory: {resolved}")
    return resolved


def validate_config(config, operation):
    """Validate fields required by a build or publisher operation."""
    project = config.project
    build = config.section("build")

    version_file = project_path(
        config, project["version_file"], "[project].version_file", kind="file"
    )
    if operation == "build":
        for key in ("description", "maintainer", "license"):
            _require_string(project, key, "[project]")
        for name in ("deb", "rpm"):
            if name not in config.data:
                _config_error(f"missing [{name}] section required by package.py")

        platforms = _validate_platforms(build)
        _validate_package_name(config.section("deb"), project["id"], "[deb]")
        _validate_package_name(config.section("rpm"), project["id"], "[rpm]")
        deb_architectures = _validate_deb_architectures(config.section("deb"))
        expected_architectures = [
            platform.removeprefix("linux/") for platform in platforms
        ]
        if deb_architectures != expected_architectures:
            _config_error(
                "[deb].architectures must match [build].platforms in the same order"
            )

        args = build.get("args", {})
        if not isinstance(args, dict) or any(
            not isinstance(key, str) or not isinstance(value, (str, int, float, bool))
            for key, value in args.items()
        ):
            _config_error("[build].args must contain scalar values")

        project_path(
            config,
            build.get("context", "."),
            "[build].context",
            allow_root=True,
            kind="directory",
        )
        project_path(
            config,
            build.get("dockerfile", "Dockerfile"),
            "[build].dockerfile",
            kind="file",
        )
        project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
        _validate_assets(config)
        _validate_string_list(config.section("deb"), "depends", "[deb]")
        _validate_string_list(config.section("rpm"), "requires", "[rpm]")
        _validate_hosting(config)

    elif operation == "apt":
        _validate_package_name(config.section("deb"), project["id"], "[deb]")
        _validate_deb_architectures(config.section("deb"))
        project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
        _validate_hosting(config)
    elif operation == "rpm":
        _validate_package_name(config.section("rpm"), project["id"], "[rpm]")
        _validate_platforms(build)
        release = config.section("rpm").get("release", "1")
        if not SAFE_VERSION.fullmatch(str(release)):
            _config_error("[rpm].release contains unsupported characters")
        project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
        _validate_hosting(config)
    elif operation == "aur":
        _validate_package_name(
            config.section("aur"), project["id"], "[aur]", optional=True
        )
        project_path(config, build.get("output_dir", "dist"), "[build].output_dir")
        if config.section("aur").get("git_pkgbuild"):
            project_path(
                config,
                config.section("aur")["git_pkgbuild"],
                "[aur].git_pkgbuild",
                kind="file",
            )
        _validate_string_list(config.section("aur"), "depends", "[aur]")
        helper_image = config.section("aur").get("srcinfo_helper_image")
        if helper_image is not None and (
            not isinstance(helper_image, str) or not PINNED_IMAGE.fullmatch(helper_image)
        ):
            _config_error(
                "[aur].srcinfo_helper_image must be a Docker image pinned by a sha256 digest"
            )
        _validate_hosting(config)
    else:
        raise ValueError(f"Unknown validation operation: {operation}")

    return version_file


def _validate_package_name(section, default, label, optional=False):
    keys = (
        ("package_name", "git_package", "binary_package")
        if label == "[aur]"
        else ("package_name",)
    )
    for key in keys:
        value = section.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
            _config_error(f"{label}.{key} contains unsupported characters")
    if not optional and not SAFE_IDENTIFIER.fullmatch(
        section.get("package_name", default)
    ):
        _config_error(f"{label}.package_name contains unsupported characters")


def _validate_deb_architectures(section):
    architectures = section.get("architectures", ["amd64"])
    if (
        not isinstance(architectures, list)
        or not architectures
        or any(arch not in SUPPORTED_DEB_ARCHITECTURES for arch in architectures)
        or len(architectures) != len(set(architectures))
    ):
        allowed = ", ".join(SUPPORTED_DEB_ARCHITECTURES)
        _config_error(f"[deb].architectures must contain unique values from: {allowed}")
    return architectures


def _validate_platforms(build):
    platforms = build.get("platforms", ["linux/amd64"])
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(platform not in SUPPORTED_PLATFORMS for platform in platforms)
        or len(platforms) != len(set(platforms))
    ):
        allowed = ", ".join(SUPPORTED_PLATFORMS)
        _config_error(f"[build].platforms must contain unique values from: {allowed}")
    return platforms


def _validate_assets(config):
    package = config.section("package")
    binary = package.get("binary", config.project["id"])
    project_path(config, binary, "[package].binary")
    for kind in ("deb", "rpm", "archive"):
        subsection = package.get(kind, {})
        if not isinstance(subsection, dict):
            _config_error(f"[package.{kind}] must be a table")
        assets = subsection.get("assets", [])
        if not isinstance(assets, list):
            _config_error(f"[package.{kind}].assets must be an array of tables")
        for index, asset in enumerate(assets):
            label = f"[package.{kind}].assets[{index}]"
            if not isinstance(asset, dict):
                _config_error(f"{label} must be a table")
            source = _require_string(asset, "source", label)
            destination = _require_string(asset, "dest", label)
            project_path(config, source, f"{label}.source")
            dest_path = Path(destination)
            if not dest_path.is_absolute() or ".." in dest_path.parts:
                _config_error(f"{label}.dest must be an absolute package path")
            mode = asset.get("mode", 0o644)
            if not isinstance(mode, int) or mode < 0 or mode > 0o7777:
                _config_error(f"{label}.mode must be a valid integer file mode")

    appimage = package.get("appimage", {})
    if not isinstance(appimage, dict):
        _config_error("[package.appimage] must be a table")
    for key in ("icon", "desktop"):
        if key in appimage:
            project_path(
                config, appimage[key], f"[package.appimage].{key}", kind="file"
            )


def _validate_string_list(section, key, label):
    values = section.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        _config_error(f"{label}.{key} must be an array of non-empty strings")


def _validate_hosting(config):
    hosting = config.section("hosting")
    for name in ("apt", "aur", "signing"):
        section = hosting.get(name, {})
        if not isinstance(section, dict):
            _config_error(f"[hosting.{name}] must be a table")
    for key, default in (
        ("release_prefix", "releases"),
        ("apt_prefix", "apt"),
        ("rpm_prefix", "rpm"),
    ):
        value = hosting.get(key, default)
        path = Path(value) if isinstance(value, str) else Path("..")
        if (
            not isinstance(value, str)
            or not value.strip("/")
            or value.startswith("/")
            or ".." in path.parts
        ):
            _config_error(f"[hosting].{key} must be a safe object prefix")

    apt = hosting.get("apt", {})
    for key in ("suite", "component", "origin", "label"):
        if key in apt:
            _require_string(apt, key, "[hosting.apt]")

    aur = hosting.get("aur", {})
    for key in ("host", "ssh_user", "maintainer"):
        if key in aur:
            _require_string(aur, key, "[hosting.aur]")
    host = aur.get("host", "aur.archlinux.org")
    user = aur.get("ssh_user", "aur")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", user
    ):
        _config_error("[hosting.aur] host or ssh_user contains unsupported characters")


def project_version(config):
    """Read the project version using the configured version file."""
    version_path = project_path(
        config,
        config.project["version_file"],
        "[project].version_file",
        kind="file",
    )
    version = version_path.read_text().strip()
    if not SAFE_VERSION.fullmatch(version):
        _config_error("project version contains unsupported characters")
    return version


def reject_published_release(existing_keys, release_keys, package_name, version):
    """Reject a release when any of its immutable package objects already exist."""
    published = sorted(set(existing_keys).intersection(release_keys))
    if published:
        names = ", ".join(published)
        raise ValueError(
            f"{package_name} {version} is already published; "
            f"existing object(s): {names}"
        )


def release_marker_key(prefix, package_name, version):
    """Return the permanent marker key for a published package version."""
    return f"{prefix}/.published/{package_name}/{version}.published"


def write_release_marker(prefix, package_name, version):
    """Record a successfully published version in R2."""
    client, bucket = r2_client()
    key = release_marker_key(prefix, package_name, version)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=f"{package_name} {version}\n".encode(),
        ContentType="text/plain",
        CacheControl="public, max-age=31536000, immutable",
    )
    return key


def compute_hashes(path):
    """Return MD5, SHA-1, SHA-256, and byte size for a file."""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0

    with open(path, "rb") as file:
        while chunk := file.read(65536):
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)

    return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest(), size


def load_env_file(path):
    """Load KEY=VALUE entries without replacing existing environment values."""
    env_file = Path(path)
    if not env_file.is_file():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    loaded = 0
    with env_file.open() as file:
        for lineno, raw in enumerate(file, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(
                    f"Warning: {path}:{lineno} not a KEY=VALUE line, skipping",
                    file=sys.stderr,
                )
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            if key not in os.environ:
                os.environ[key] = value.strip()
                loaded += 1

    print(f"Loaded {loaded} variable(s) from {path}")


def check_tools(*tools):
    """Exit with a useful message when a required executable is unavailable."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        print("Missing required tool(s): " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)


def r2_client():
    """Create an R2-compatible S3 client and return it with the bucket name."""
    import boto3

    required = [
        "AWS_ENDPOINT_URL",
        "AWS_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ]
    missing = [name for name in required if name not in os.environ]
    if missing:
        print("Missing env vars: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("R2_REGION", "auto"),
    )
    return client, os.environ["AWS_BUCKET"]


def upload_file(path, key):
    """Upload one file to the configured R2 bucket."""
    client, bucket = r2_client()
    client.upload_file(str(path), bucket, key)
    print(f"  {key}")


def download_optional(key, path):
    """Download an R2 object, returning False when it does not exist."""
    client, bucket = r2_client()
    try:
        client.download_file(bucket, key, str(path))
    except client.exceptions.NoSuchKey:
        return False
    except Exception as error:
        response = getattr(error, "response", {})
        if response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            return False
        raise
    return True


def download_prefix(prefix, destination, *, suffix=None):
    """Download matching objects below a prefix, preserving relative paths."""
    client, bucket = r2_client()
    normalized = prefix.strip("/") + "/"
    destination = Path(destination)
    downloaded = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=normalized):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/") or (suffix and not key.endswith(suffix)):
                continue
            relative = Path(key.removeprefix(normalized))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe object key below {prefix}: {key}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            downloaded.append(target)
    return downloaded


def list_prefix(prefix, *, suffix=None):
    """List object keys below an R2 prefix without downloading their bodies."""
    client, bucket = r2_client()
    normalized = prefix.strip("/") + "/"
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=normalized):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/") or (suffix and not key.endswith(suffix)):
                continue
            keys.append(key)
    return keys


def delete_keys(keys):
    """Delete R2 objects in batches, ignoring an empty key list."""
    if not keys:
        return
    client, bucket = r2_client()
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )


def acquire_r2_lock(key, *, max_age=1800):
    """Acquire an expiring single-writer lock in R2.

    R2 conditional creation makes competing publishers fail instead of
    publishing from stale metadata. A stale lock from a crashed publisher is
    recoverable after max_age seconds.
    """
    import time

    client, bucket = r2_client()
    token = uuid.uuid4().hex

    for attempt in range(2):
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=token.encode(),
                IfNoneMatch="*",
                ContentType="text/plain",
            )
            print(f"Acquired R2 publish lock: {key}")

            def release():
                current = client.get_object(Bucket=bucket, Key=key)
                owner = current["Body"].read().decode()
                if owner == token:
                    client.delete_object(Bucket=bucket, Key=key)
                    print(f"Released R2 publish lock: {key}")

            return release
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"}:
                raise

            head = client.head_object(Bucket=bucket, Key=key)
            age = time.time() - head["LastModified"].timestamp()
            if age <= max_age:
                raise RuntimeError(
                    f"R2 publish lock is held: {key} ({int(max_age - age)}s remaining)"
                ) from error

            print(f"Taking over stale R2 publish lock: {key}")
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=token.encode(),
                    IfMatch=head["ETag"],
                    ContentType="text/plain",
                )
            except Exception as takeover_error:
                response = getattr(takeover_error, "response", {})
                code = response.get("Error", {}).get("Code")
                if code in {"PreconditionFailed", "412"}:
                    continue
                raise

            print(f"Acquired R2 publish lock: {key}")

            def release():
                current = client.get_object(Bucket=bucket, Key=key)
                owner = current["Body"].read().decode()
                if owner == token:
                    client.delete_object(Bucket=bucket, Key=key)
                    print(f"Released R2 publish lock: {key}")

            return release

    raise RuntimeError(f"Could not acquire R2 publish lock: {key}")


def upload_tree(root_dir, *, order=None, extra_args=None, skip_existing=None):
    """Upload a repository tree, optionally skipping immutable existing keys."""
    client, bucket = r2_client()
    root_dir = Path(root_dir)
    paths = [path for path in root_dir.rglob("*") if path.is_file()]
    if order:
        paths.sort(key=lambda path: (order(path.relative_to(root_dir)), str(path)))
    else:
        paths.sort()
    skip_existing = set(skip_existing or ())
    for path in paths:
        relative = path.relative_to(root_dir)
        key = str(relative)
        if key in skip_existing:
            print(f"  {key} (unchanged)")
            continue
        options = extra_args(relative) if extra_args else None
        if options:
            client.upload_file(str(path), bucket, key, ExtraArgs=options)
        else:
            client.upload_file(str(path), bucket, key)
        print(f"  {key}")


def run(command, **kwargs):
    """Run a command and print it first."""
    print("  $ " + " ".join(str(part) for part in command))
    subprocess.run(command, check=True, **kwargs)
