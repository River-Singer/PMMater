"""Build or verify a deterministic, source-fingerprinted pmmaster Skill archive.

Run from any directory. Only project package sources and the explicit fictional
demo database are eligible; runtime folders, credentials and backups are not.
Existing releases with different bytes are never overwritten: choose a new name.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "project"
TOP_DIRS = {"pm_core", "data", "docs", "references", "tests", "demo"}
ROOT_FILES = {
    "SKILL.md", "README.md", "LICENSE", "使用手册.md", "pmmaster_导出说明.md",
    "requirements.txt", "项目管理模板.xlsx", "install_weekly_task.ps1", "run_demo.bat",
}
SKIP_DIRS = {"__pycache__", ".venv", "venv", "workspace", "node_modules", ".pytest_cache"}
EXTENSIONS = {".py", ".md", ".json", ".html", ".csv", ".sql", ".txt", ".ps1", ".bat", ".xlsx"}
SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?:app_secret|access_token|refresh_token|client_secret)[\"']?\s*[:=]\s*[\"']"
               rb"(?!<|\$|YOUR|your|example|test|fake|token)[A-Za-z0-9_-]{24,}[\"']", re.I),
)
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def is_link(path: Path) -> bool:
    """Do not traverse symlinks or Windows junctions into unrelated files."""
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def package_files(source: Path) -> dict[str, bytes]:
    content = {}
    for directory, folders, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        folders[:] = sorted(name for name in folders if name not in SKIP_DIRS
                            and not name.startswith(".") and not is_link(parent / name)
                            and (parent != source or name in TOP_DIRS))
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(source).as_posix()
            if is_link(path) or name.startswith(".") or name.startswith("~$"):
                continue
            if parent == source and name not in ROOT_FILES:
                continue
            if ".backup" in name or ".bak" in name:
                continue
            if path.suffix.lower() not in EXTENSIONS and relative not in {"LICENSE", "demo/demo.sqlite3"}:
                continue
            # Real exports/configs belong in workspace/, not a distributable demo.
            if name.lower().startswith(("credential", "secret")):
                raise ValueError(f"Credential file in distributable source: {relative}")
            raw = path.read_bytes()
            if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
                raise ValueError(f"Possible credential in distributable source: {relative}")
            content[f"pmmaster/{relative}"] = raw
    required = {f"pmmaster/{name}" for name in ROOT_FILES}
    required.update({"pmmaster/pm_core/__init__.py", "pmmaster/data/schema.json"})
    missing = sorted(required - content.keys())
    if missing:
        raise ValueError(f"Missing required package files: {', '.join(missing)}")
    return dict(sorted(content.items()))


def verify_license(root_license: Path, content: dict[str, bytes]) -> None:
    """Match the repository notice to the exact bytes selected for the archive."""
    if is_link(root_license) or not root_license.is_file():
        raise ValueError("Repository LICENSE must be a regular file")
    notice = root_license.read_bytes()
    if not notice.strip() or content.get("pmmaster/LICENSE") != notice:
        raise ValueError("Repository LICENSE and project/LICENSE must be nonempty and byte-identical")


def build_manifest(content: dict[str, bytes], name: str) -> dict:
    rows = [{"path": path, "bytes": len(raw), "sha256": sha256(raw).hexdigest()}
            for path, raw in content.items()]
    fingerprint = sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()
    schema = json.loads(content["pmmaster/data/schema.json"])
    return {
        "release": name,
        "schema_version": schema["schema_version"],
        "source_directory": "project",
        "source_tree_sha256": fingerprint,
        "archive": f"{name}.zip",
        "files": rows,
        "build": {"format": "zip", "compression": "deflate-9", "entry_time": "1980-01-01T00:00:00",
                  "entry_mode": "0644", "ordering": "path ascending"},
        "scope": "Skill sources, template and fictional demos; no real tenant deployment or enabled scheduler.",
    }


def write_archive(path: Path, content: dict[str, bytes]) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in content.items():
            entry = ZipInfo(name, date_time=ZIP_TIME)
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, raw, compress_type=ZIP_DEFLATED, compresslevel=9)


def verify_archive(path: Path, content: dict[str, bytes]) -> None:
    with ZipFile(path) as archive:
        if archive.testzip() is not None or archive.namelist() != list(content):
            raise ValueError("Archive structure does not match the current source")
        for name, expected in content.items():
            if archive.read(name) != expected:
                raise ValueError(f"Archive differs from current source: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Release name without extension; use a new name for changed content")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--check", action="store_true", help="Verify an existing archive and manifest against current project sources")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.name):
        parser.error("--name must be a plain file stem with letters, digits, dots, hyphens or underscores")
    content = package_files(SOURCE)
    verify_license(ROOT / "LICENSE", content)
    manifest = build_manifest(content, args.name)
    archive_path = args.out_dir / manifest["archive"]
    manifest_path = args.out_dir / f"{args.name}.manifest.json"
    if args.check:
        verify_archive(archive_path, content)
        manifest["sha256"] = sha256(archive_path.read_bytes()).hexdigest()
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Manifest differs from the current archive/source metadata")
    else:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pmmaster-build-") as temporary:
            staged = Path(temporary) / archive_path.name
            write_archive(staged, content)
            verify_archive(staged, content)
            raw = staged.read_bytes()
            manifest["sha256"] = sha256(raw).hexdigest()
            manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            outputs = {archive_path: raw, manifest_path: manifest_raw}
            for destination, value in outputs.items():
                if destination.exists() and destination.read_bytes() != value:
                    raise FileExistsError(f"Release already exists with different content; choose a new --name: {destination.name}")
            for destination, value in outputs.items():
                if not destination.exists():
                    with destination.open("xb") as stream:
                        stream.write(value)
    print(json.dumps({"ok": True, "checked": args.check, "archive": str(archive_path),
                      "files": len(content), "sha256": manifest["sha256"],
                      "source_tree_sha256": manifest["source_tree_sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
