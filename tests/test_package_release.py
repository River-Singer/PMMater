"""Repository-only release tests; the installed Skill does not need build tools."""
from contextlib import redirect_stdout
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_release", REPO / "scripts/package_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseLicenseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "project"
        self.source.mkdir()
        for name in release.ROOT_FILES:
            (self.source / name).write_bytes(b"fixture\n")
        for name, content in {"pm_core/__init__.py": b"", "data/schema.json": b'{"schema_version":7}'}.items():
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        self.notice = "Example fixture notice. 版权声明。\n".encode()
        (self.root / "LICENSE").write_bytes(self.notice)
        (self.source / "LICENSE").write_bytes(self.notice)
        self.output = self.root / "release"

    def run_cli(self, check=False):
        argv = ["package_release.py", "--name", "fixture-v7"] + (["--check"] if check else [])
        with patch.object(release, "ROOT", self.root), patch.object(release, "SOURCE", self.source), \
                patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            release.main()

    def test_build_and_check_include_exact_license_and_manifest_digest(self):
        # The new extensionless exception must not admit unrelated files.
        (self.source / "UNEXPECTED").write_bytes(b"not distributable")
        nested = self.source / "docs"
        nested.mkdir()
        (nested / "UNEXPECTED").write_bytes(b"not distributable either")
        (nested / "LICENSE").write_bytes(b"not the root package notice")
        self.run_cli()
        self.run_cli(check=True)
        with ZipFile(self.output / "fixture-v7.zip") as archive:
            self.assertEqual(self.notice, archive.read("pmmaster/LICENSE"))
            self.assertNotIn("pmmaster/UNEXPECTED", archive.namelist())
            self.assertNotIn("pmmaster/docs/UNEXPECTED", archive.namelist())
            self.assertNotIn("pmmaster/docs/LICENSE", archive.namelist())
        manifest = json.loads((self.output / "fixture-v7.manifest.json").read_text(encoding="utf-8"))
        notice = next(row for row in manifest["files"] if row["path"] == "pmmaster/LICENSE")
        self.assertEqual(len(self.notice), notice["bytes"])
        self.assertEqual(sha256(self.notice).hexdigest(), notice["sha256"])

    def test_missing_package_notice_fails_build_and_check_before_output(self):
        (self.source / "LICENSE").unlink()
        for check in (False, True):
            with self.subTest(check=check), self.assertRaisesRegex(ValueError, "Missing required.*LICENSE"):
                self.run_cli(check=check)
        self.assertFalse(self.output.exists())

    def test_missing_root_empty_and_mismatched_notices_fail_both_modes(self):
        root_notice, package_notice = self.root / "LICENSE", self.source / "LICENSE"
        cases = ((None, self.notice), (b" \n", b" \n"), (b"different\n", self.notice))
        for root_bytes, package_bytes in cases:
            package_notice.write_bytes(package_bytes)
            if root_bytes is None:
                root_notice.unlink()
            else:
                root_notice.write_bytes(root_bytes)
            for check in (False, True):
                with self.subTest(root_bytes=root_bytes, check=check), self.assertRaisesRegex(ValueError, "LICENSE"):
                    self.run_cli(check=check)
        self.assertFalse(self.output.exists())

    def test_changed_license_requires_new_release_name(self):
        self.run_cli()
        archive_path = self.output / "fixture-v7.zip"
        manifest_path = self.output / "fixture-v7.manifest.json"
        original = archive_path.read_bytes(), manifest_path.read_bytes()
        revised = self.notice + b"Revision\n"
        (self.root / "LICENSE").write_bytes(revised)
        (self.source / "LICENSE").write_bytes(revised)
        with self.assertRaisesRegex(FileExistsError, "choose a new --name"):
            self.run_cli()
        self.assertEqual(original, (archive_path.read_bytes(), manifest_path.read_bytes()))
        with self.assertRaisesRegex(ValueError, "Archive differs.*LICENSE"):
            self.run_cli(check=True)


if __name__ == "__main__":
    unittest.main()
