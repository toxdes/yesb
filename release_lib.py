"""Shared helpers for yesb release scripts."""

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


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
    """Load and minimally validate a project's release.toml."""
    config_path = Path(path or "release.toml").resolve()
    if not config_path.is_file():
        print(f"Error: {config_path} not found", file=sys.stderr)
        sys.exit(1)

    with config_path.open("rb") as file:
        data = tomllib.load(file)

    project = data.get("project")
    if not isinstance(project, dict):
        print(f"Error: {config_path} is missing [project]", file=sys.stderr)
        sys.exit(1)

    missing = [name for name in ("id", "version_file") if name not in project]
    if missing:
        names = ", ".join(missing)
        print(f"Error: [project] is missing: {names}", file=sys.stderr)
        sys.exit(1)

    return ProjectConfig(data=data, path=config_path)


def project_version(config):
    """Read the project version using the configured version file."""
    version_path = config.root / config.project["version_file"]
    if not version_path.is_file():
        print(f"Error: version file {version_path} not found", file=sys.stderr)
        sys.exit(1)
    return version_path.read_text().strip()


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


def upload_tree(root_dir):
    """Upload every file below root_dir using paths relative to root_dir."""
    client, bucket = r2_client()
    for path in sorted(root_dir.rglob("*")):
        if path.is_file():
            key = str(path.relative_to(root_dir))
            client.upload_file(str(path), bucket, key)
            print(f"  {key}")


def run(command, **kwargs):
    """Run a command and print it first."""
    print("  $ " + " ".join(str(part) for part in command))
    subprocess.run(command, check=True, **kwargs)
