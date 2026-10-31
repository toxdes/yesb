import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from release_aur import generate_srcinfo, main, push_aur, release_bin, release_git, release_source, validate_relationships
from release_lib import ProjectConfig, aur_relationships, load_config, validate_config


class AurRelationshipsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ProjectConfig({
            "project": {"id": "example", "version_file": "VERSION"},
            "aur": {"git_pkgbuild": "PKGBUILD"},
        }, self.root / "release.toml")
        (self.root / "VERSION").write_text("1.2.3\n")
        self.required = {"provides": ["example=1.2.3"], "conflicts": ["example"]}

    def test_defaults_overrides_and_canonical_name(self):
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example-bin"), self.required)
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example-git"), self.required)
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example"), {"provides": [], "conflicts": []})
        self.config.data["aur"]["package_name"] = "other"
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example-bin"), {
            "provides": ["other=1.2.3"], "conflicts": ["other"],
        })
        self.config.data["aur"].update(provides=["custom={version}", "interface"], conflicts=["custom<2.0"])
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example-bin"), {
            "provides": ["custom=1.2.3", "interface"], "conflicts": ["custom<2.0"],
        })
        self.config.data["aur"].update(provides=[], conflicts=[])
        self.assertEqual(aur_relationships(self.config, "1.2.3", "example-bin"), {"provides": [], "conflicts": []})

    def test_invalid_overrides_fail_manifest_validation(self):
        for key, value in (
            ("provides", "example"), ("provides", ["example>{version}"]),
            ("provides", ["example=$(uname)"]), ("provides", ["example={unknown}"]),
            ("conflicts", ["example-bin"]), ("conflicts", ["example\nother"]),
        ):
            with self.subTest(key=key, value=value):
                self.config.data["aur"] = {key: value}
                with self.assertRaises(SystemExit):
                    validate_config(self.config, "aur")

    def test_binary_recipe_expands_version_and_conflicts(self):
        archives = {arch: (self.root / f"example_1.2.3_{arch}.tar.gz", "a" * 64) for arch in ("x86_64", "aarch64")}
        with patch("release_aur.push_aur") as push:
            release_bin(self.config, "1.2.3", archives, aur_host="aur.archlinux.org", aur_user="aur", maintainer="maintainer")
        text = push.call_args.args[1]
        self.assertEqual(push.call_args.kwargs["relationships"], self.required)
        recipe = self.root / "PKGBUILD"
        recipe.write_text(text)
        result = subprocess.run([
            "bash", "-c", 'source "$1"; printf "%s\\n" "${provides[@]}" "${conflicts[@]}"',
            "bash", str(recipe),
        ], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(), ["example=1.2.3", "example"])
        if shutil.which("makepkg"):
            result = subprocess.run(["makepkg", "--printsrcinfo"], cwd=self.root, check=True, capture_output=True, text=True)
            validate_relationships(result.stdout, "example-bin", self.required)

    def test_git_recipe_is_preserved_and_relationships_are_required(self):
        original = 'pkgname=example-git\npkgver=0.1\nprovides=("example=${pkgver}")\nconflicts=(example)\n'
        (self.root / "PKGBUILD").write_text(original)
        with patch("release_aur.push_aur") as push:
            release_git(self.config, "1.2.3", aur_host="aur.archlinux.org", aur_user="aur", maintainer="maintainer")
        self.assertEqual((self.root / "PKGBUILD").read_text(), original)
        self.assertEqual(push.call_args.args[0], "example-git")
        self.assertIn("pkgver=1.2.3", push.call_args.args[1])
        self.assertEqual(push.call_args.kwargs["relationships"], self.required)

    def test_stable_source_recipe_pins_tag_and_has_no_self_relationships(self):
        self.config.data["aur"]["source_pkgbuild"] = "PKGBUILD.source"
        original = 'pkgname=example\npkgver=0.1\npkgrel=1\narch=(x86_64)\nsource=("git+https://example.com/example.git#tag=v${pkgver}")\nsha256sums=(SKIP)\n'
        (self.root / "PKGBUILD.source").write_text(original)
        with patch("release_aur.push_aur") as push:
            release_source(self.config, "1.2.3", aur_host="aur.archlinux.org", aur_user="aur", maintainer="maintainer")
        self.assertEqual((self.root / "PKGBUILD.source").read_text(), original)
        self.assertEqual(push.call_args.args[0], "example")
        self.assertEqual(push.call_args.kwargs["relationships"], {"provides": [], "conflicts": []})
        (self.root / "PKGBUILD").write_text(push.call_args.args[1])
        result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s\\n" "${source[@]}"', "bash", str(self.root / "PKGBUILD")], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "git+https://example.com/example.git#tag=v1.2.3")
        if shutil.which("makepkg"):
            result = subprocess.run(["makepkg", "--printsrcinfo"], cwd=self.root, check=True, capture_output=True, text=True)
            validate_relationships(result.stdout, "example", push.call_args.kwargs["relationships"])

    def test_stable_source_rejects_moving_version_function(self):
        self.config.data["aur"]["source_pkgbuild"] = "PKGBUILD.source"
        (self.root / "PKGBUILD.source").write_text("pkgver=1.2.3\npkgver() { echo moving; }\n")
        with patch("release_aur.push_aur") as push, self.assertRaisesRegex(RuntimeError, "must not define pkgver"):
            release_source(self.config, "1.2.3", aur_host="aur.archlinux.org", aur_user="aur", maintainer="maintainer")
        push.assert_not_called()

    def test_missing_source_template_fails_validation(self):
        self.config.data["aur"]["source_pkgbuild"] = "missing"
        with self.assertRaises(SystemExit):
            validate_config(self.config, "aur")

    def test_source_only_cli_does_not_upload_or_publish_other_variants(self):
        self.config.data["project"]["maintainer"] = "Maintainer <maintainer@example.com>"
        self.config.data["aur"] = {
            "source_pkgbuild": "PKGBUILD.source",
            "srcinfo_helper_image": "example/aur-helper@sha256:" + "a" * 64,
        }
        (self.root / "PKGBUILD.source").write_text("pkgname=example\npkgver=1.2.3\n")
        with (
            patch("sys.argv", ["release_aur.py", "--type", "source"]),
            patch("release_aur.load_config", return_value=self.config),
            patch("release_aur.check_tools"),
            patch("release_aur.release_source") as source,
            patch("release_aur.release_git") as git,
            patch("release_aur.release_archives") as archives,
            patch("release_aur.upload_releases") as upload,
            patch("release_aur.release_bin") as binary,
        ):
            main()
        source.assert_called_once()
        for other in (git, archives, upload, binary):
            other.assert_not_called()

    def test_srcinfo_inheritance_and_package_overrides(self):
        base = "pkgbase = example-git\nprovides = example=1.2.3\nconflicts = example\n"
        validate_relationships(base + "pkgname = example-git\n", "example-git", self.required)
        for fields in (
            "provides = example\n", "provides = example=0.1\n", "conflicts =\n",
            "provides = example=1.2.3\nconflicts = example-git\n",
        ):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                validate_relationships(base + "pkgname = example-git\n" + fields, "example-git", self.required)
        with self.assertRaises(RuntimeError):
            validate_relationships(base + "pkgname = other\n", "example-git", self.required)

    def test_missing_git_relationships_stop_before_commit_or_push(self):
        srcinfo = "pkgbase = example-git\npkgver = 1.2.3\npkgname = example-git\n"
        with (
            patch("release_aur.clone_or_pull", return_value=self.root),
            patch("release_aur.generate_srcinfo", return_value=srcinfo),
            patch("release_aur.run") as run,
            self.assertRaisesRegex(RuntimeError, "missing provides"),
        ):
            push_aur("example-git", "pkgver=1.2.3\n", aur_host="aur.archlinux.org", aur_user="aur", maintainer="maintainer", version="1.2.3", relationships=self.required)
        run.assert_not_called()


class SrcinfoHelperTests(unittest.TestCase):
    def test_helper_runs_isolated_and_returns_srcinfo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PKGBUILD").write_text("pkgname = example\n")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="pkgbase = example\n", stderr=""
            )

            with patch("release_aur.subprocess.run", return_value=completed) as run:
                srcinfo = generate_srcinfo(
                    repo,
                    "registry.example.com/yesb-makepkg@sha256:" + "a" * 64,
                )

            self.assertEqual(srcinfo, "pkgbase = example\n")
            command = run.call_args.args[0]
            self.assertIn("--network=none", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertNotIn("/root/.ssh", " ".join(command))
            self.assertIn(
                "type=bind,src=" + str(repo) + ",dst=/input,readonly", command
            )
            self.assertIn(
                "/work:rw,noexec,nosuid,size=64m,uid=", " ".join(command)
            )
            self.assertEqual(
                command[-3:],
                [
                    "sh",
                    "-c",
                    "cp -- /input/PKGBUILD /work/PKGBUILD && makepkg --printsrcinfo",
                ],
            )

    def test_helper_image_must_be_digest_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.0.0\n")
            (root / "release.toml").write_text(
                "[project]\n"
                'id = "example"\n'
                'version_file = "VERSION"\n'
                "[aur]\n"
                'srcinfo_helper_image = "archlinux:latest"\n'
            )

            config = load_config(root / "release.toml")
            with self.assertRaises(SystemExit):
                validate_config(config, "aur")

            config.data["aur"]["srcinfo_helper_image"] = (
                "registry.example.com/yesb-makepkg@sha256:" + "a" * 64
            )
            validate_config(config, "aur")


if __name__ == "__main__":
    unittest.main()
