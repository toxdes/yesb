import gzip
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from release_rpm import (
    COMMON, RPMMD, build_repo, load_existing_metadata, metadata_records,
    retained_rpm_paths, validate_package_locations,
)


class RpmMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def indexes(self):
        files = {}
        entries = []
        for kind in ("primary", "filelists", "other"):
            body = gzip.compress(f"<{kind}/>".encode())
            digest = hashlib.sha256(body).hexdigest()
            href = f"repodata/{digest}-{kind}.xml.gz"
            files[f"rpm/{href}"] = body
            entries.append(f'<data type="{kind}"><checksum type="sha256">{digest}</checksum><location href="{href}"/></data>')
        files["rpm/repodata/repomd.xml"] = (f'<repomd xmlns="{RPMMD}">' + "".join(entries) + "</repomd>").encode()
        return files

    def test_only_referenced_xml_indexes_are_downloaded(self):
        files = self.indexes()

        def download(key, path):
            if key not in files:
                return False
            path.write_bytes(files[key])
            return True

        with patch("release_rpm.download_optional", side_effect=download) as fetch:
            result = load_existing_metadata("rpm", self.root, ["rpm/other-1-1.noarch.rpm"])
        self.assertEqual(result, self.root)
        self.assertEqual({call.args[0] for call in fetch.call_args_list}, set(files))
        self.assertFalse(any(call.args[0].endswith(".rpm") for call in fetch.call_args_list))

    def test_corrupt_or_missing_existing_metadata_fails(self):
        with patch("release_rpm.download_optional", return_value=False):
            self.assertIsNone(load_existing_metadata("rpm", self.root, []))
            with self.assertRaisesRegex(RuntimeError, "refusing to drop"):
                load_existing_metadata("rpm", self.root, ["rpm/other-1-1.noarch.rpm"])
        files = self.indexes()

        def download(key, path):
            path.write_bytes(files[key] if key.endswith("repomd.xml") else b"corrupt")
            return True

        with patch("release_rpm.download_optional", side_effect=download), self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            load_existing_metadata("rpm", self.root, ["rpm/other-1-1.noarch.rpm"])

    def test_only_exact_package_names_are_retired(self):
        keys = ["rpm/app-1.0-1.x86_64.rpm", "rpm/app-helper-1.0-1.x86_64.rpm", "rpm/other-2.0-1.aarch64.rpm"]
        self.assertEqual(retained_rpm_paths("rpm", keys, "app"), [
            "app-helper-1.0-1.x86_64.rpm", "other-2.0-1.aarch64.rpm",
        ])
        for key in ("rpm/../escaped-1-1.noarch.rpm", "other/app-1-1.noarch.rpm", "rpm/app\nother-1-1.noarch.rpm"):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                retained_rpm_paths("rpm", [key], "app")

    def test_metadata_locations_must_stay_inside_repodata(self):
        for href in ("../outside", "https://example.org/primary", "/repodata/primary", "repodata/subdir/primary"):
            with self.subTest(href=href):
                repomd = self.root / "repomd.xml"
                repomd.write_text(f'<repomd xmlns="{RPMMD}"><data type="primary"><location href="{href}"/></data></repomd>')
                with self.assertRaises(RuntimeError):
                    metadata_records(repomd)


@unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("createrepo_c"), "requires RPM test tools")
class RpmRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"GPG_KEY_ID": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def rpm(self, name, version):
        top = self.root / f"build-{name}-{version}"
        for folder in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
            (top / folder).mkdir(parents=True)
        spec = top / "SPECS/package.spec"
        spec.write_text(f"""Name: {name}
Version: {version}
Release: 1
Summary: Yesb integration fixture
License: MIT
BuildArch: noarch

%description
Yesb integration fixture.

%install
mkdir -p %{{buildroot}}/usr/share/{name}
echo fixture > %{{buildroot}}/usr/share/{name}/fixture

%files
/usr/share/{name}/fixture
""")
        subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], check=True, capture_output=True)
        return next((top / "RPMS/noarch").glob("*.rpm"))

    def primary(self, root):
        href, _ = metadata_records(root / "repodata/repomd.xml")["primary"]
        with gzip.open(root / href) as body:
            return {
                entry.findtext(f"{{{COMMON}}}name"): (
                    entry.find(f"{{{COMMON}}}version").get("ver"),
                    entry.findtext(f"{{{COMMON}}}checksum"),
                ) for entry in ET.parse(body).getroot()
            }

    def test_shared_repository_update_needs_no_old_rpm_payloads(self):
        packages = [self.rpm("app", "1.0"), self.rpm("app-helper", "1.0"), self.rpm("other", "2.0")]
        old = self.root / "old"
        build_repo(old, packages, "rpm", "app")
        original = self.primary(old / "rpm")
        files = {str(path.relative_to(old)): path.read_bytes() for path in (old / "rpm/repodata").iterdir() if path.is_file()}

        def download(key, path):
            if key not in files:
                return False
            path.write_bytes(files[key])
            return True

        keys = [f"rpm/{path.name}" for path in packages]
        with patch("release_rpm.download_optional", side_effect=download) as fetch:
            cache = load_existing_metadata("rpm", self.root / "cache", keys)
        self.assertEqual(len(fetch.call_args_list), 4)
        current = self.rpm("app", "3.0")
        output = self.root / "new"
        build_repo(output, [current], "rpm", "app", existing_metadata=cache, existing_package_keys=keys)
        actual = self.primary(output / "rpm")
        self.assertEqual(actual["app"][0], "3.0")
        self.assertEqual(actual["other"], original["other"])
        self.assertEqual(actual["app-helper"], original["app-helper"])
        self.assertEqual(set(actual), {"app", "app-helper", "other"})
        self.assertEqual([path.name for path in (output / "rpm").glob("*.rpm")], [current.name])
        with self.assertRaises(RuntimeError):
            validate_package_locations(output / "rpm", [current.name])


if __name__ == "__main__":
    unittest.main()
