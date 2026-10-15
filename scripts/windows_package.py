"""Shared, testable Windows release policy. No network calls or application imports."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import types
import zipfile
from urllib.parse import urlsplit, parse_qs

from stage_tools import FORBIDDEN_EXTS, FORBIDDEN_NAMES, UNUSED_PACKAGES, MANIFEST

SOURCE_DIRS = {"atelier", "gui", "scripts", "tests", "docs", "licenses", "images", "linux", ".github"}
ROOT_SUFFIXES = {".py", ".md", ".txt", ".ps1", ".bat", ".spec", ".iss", ".ico", ".png"}
DENIED_DIRS = {".git", ".venv", ".venv-build", "__pycache__", "_webview", "_cache", "_logs",
               "mappings", "projects", "exported", "imported", "retrieval", "tools", "assets"}
DENIED_FILES = FORBIDDEN_NAMES | {"credentials.json", "secrets.json", ".pypirc", "id_rsa", "id_ed25519"}
DENIED_EXTS = FORBIDDEN_EXTS | {".pem", ".key", ".pfx", ".p12", ".env"}
TOP_LEVEL = {"Atelier.exe", "_internal", "Tools", "LICENSE", "README.txt", "licenses",
             "MANIFEST.json", "BUILD_INFO.json", "NATIVE_TOOLS.json"}


class BuildError(RuntimeError):
    pass


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def safe_file(root, relative):
    original = str(relative)
    relative = PurePosixPath(relative)
    if original != relative.as_posix() or relative.is_absolute() or not relative.parts or any(p in {"..", "."} or p.rstrip(" .") != p or ":" in p or "\\" in p for p in relative.parts):
        raise BuildError(f"Unsafe relative path: {relative}")
    path = root.joinpath(*relative.parts)
    for part in [path, *path.parents]:
        if part == root.parent:
            break
        if part.is_symlink() or part.is_junction():
            raise BuildError(f"Links/junctions cannot be packaged: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise BuildError(f"Missing or unsafe file: {relative}")
    return path


def forbidden(relative, source=False):
    rel = PurePosixPath(relative)
    parts = [p.lower() for p in rel.parts]
    name = parts[-1]
    if name in DENIED_FILES or name.startswith(".env") or rel.suffix.lower() in DENIED_EXTS:
        return True
    # Frozen third-party packages may legitimately have assets/ or tools/ directories.
    dirs = set(parts[:-1])
    if not source and parts[0] == "_internal":
        dirs -= {"assets", "tools", "__pycache__"}
    if not source:
        dirs.discard("tools")
    return bool(dirs & DENIED_DIRS)


def source_allowed(name):
    rel = PurePosixPath(name)
    return (rel.parts[0] in SOURCE_DIRS or
            (len(rel.parts) == 1 and (rel.suffix.lower() in ROOT_SUFFIXES or name in {"version", "LICENSE", ".gitignore", ".gitattributes"})))


def source_identity(root):
    if not (root / ".git").exists() and (root / "SOURCE_MANIFEST.json").is_file():
        info = json.loads(safe_file(root, "BUILD_INFO.json").read_text(encoding="utf-8"))
        commit = info.get("base_commit", "")
        if not re.fullmatch("[0-9a-f]{40}", commit):
            raise BuildError("Source archive has invalid base-commit provenance.")
        return commit, bool(info.get("working_tree")), "source-archive"
    try:
        return (git(root, "rev-parse", "HEAD").decode().strip(),
                bool(git(root, "status", "--porcelain", "--untracked-files=all").strip()), "git-checkout")
    except subprocess.CalledProcessError as error:
        raise BuildError("Build from a Git checkout or the complete matching source archive.") from error


def exported_inventory(root):
    records = json.loads(safe_file(root, "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    inventory, seen = {}, set()
    for record in records:
        name = record["path"]
        if name.casefold() in seen or forbidden(name, source=True) or (name != "BUILD_INFO.json" and not source_allowed(name)):
            raise BuildError(f"Unsafe/duplicate source manifest entry: {name}")
        seen.add(name.casefold())
        path = safe_file(root, name)
        if sha256(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise BuildError(f"Source archive was modified: {name}. Use a Git checkout for edits.")
        if name != "BUILD_INFO.json":
            inventory[name] = record["sha256"]
    # Venvs and generated caches may exist after bootstrapping, but extra source must not
    # be silently ignored when someone edits the exported tree.
    for child in root.iterdir():
        if child.name in SOURCE_DIRS:
            candidates = child.rglob("*")
        elif child.is_file() and source_allowed(child.name):
            candidates = [child]
        else:
            continue
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if path.is_file() and not forbidden(rel, source=True) and rel not in inventory:
                raise BuildError(f"Unlisted source file in exported archive: {rel}")
    return inventory


def source_inventory(root):
    if not (root / ".git").exists() and (root / "SOURCE_MANIFEST.json").is_file():
        result = exported_inventory(root)
        names = []
    else:
        result = {}
        names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
    excluded = []
    for name in sorted(set(filter(None, names))):
        rel = PurePosixPath(name)
        allowed = source_allowed(name)
        if not allowed or forbidden(name, source=True):
            excluded.append(name)
            continue
        path = root / name
        if path.exists():  # Tracked deletions are part of the working-tree snapshot.
            result[name] = sha256(safe_file(root, name))
    for required in ("window.py", "Atelier.spec", "Atelier.iss", "version", "requirements-build.txt", "LICENSE"):
        if required not in result:
            raise BuildError(f"Source snapshot is missing {required}")
    return result, excluded


def inventory_digest(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def snapshot_source(root, destination, inventory, version):
    destination.mkdir(parents=True, exist_ok=False)
    for name, digest in inventory.items():
        source = safe_file(root, name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha256(target) != digest:
            raise BuildError(f"Source changed while snapshotting: {name}")
    (destination / "version").write_text(version + "\n", encoding="utf-8")


def pinned_requirements(path):
    from packaging.requirements import Requirement
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            result.update(pinned_requirements(path.parent / line[3:].strip()))
            continue
        req = Requirement(line)
        pins = list(req.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise BuildError(f"Unpinned dependency: {line}")
        if not req.marker or req.marker.evaluate():
            result[req.name] = pins[0].version
    return result


def validate_dependencies(root):
    versions = pinned_requirements(root / "requirements-dev.txt")
    for name, expected in versions.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != expected:
            raise BuildError(f"{name}: expected {expected}, found {actual}. Run scripts/bootstrap_windows.ps1.")
    return versions


def discover_iscc(explicit=None):
    if explicit and not explicit.is_file():
        raise BuildError(f"--iscc does not point to a compiler: {explicit}")
    candidates = [explicit, os.environ.get("ISCC"), shutil.which("ISCC.exe")]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = Path(os.environ.get(env, "C:/nonexistent"))
        for suffix in ("Inno Setup 6/ISCC.exe", "Inno Setup 7/ISCC.exe", "Programs/Inno Setup 6/ISCC.exe", "AtelierBuildTools/Inno Setup 6/ISCC.exe"):
            candidates.append(base / suffix)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise BuildError("Inno Setup compiler not found. Run scripts/bootstrap_windows.ps1, set ISCC, or pass --iscc PATH.")


def approved_tools(policy_path, source):
    if not policy_path:
        return []
    if not source or not source.is_dir():
        raise BuildError("--native-policy requires an existing --tools-dir.")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    records = policy.get("files", [])
    if not records:
        raise BuildError("Native policy has no approved files; omit it for an external-tools build.")
    allowed = {p for p, _ in MANIFEST["windows"] if Path(p).suffix}
    seen = set()
    for record in records:
        rel = record.get("path", "")
        # Whole vendor directories are never trusted: enumerate each approved member.
        if rel.casefold() in seen or forbidden("Tools/" + rel) or not (rel in allowed or rel.startswith("AtelierMesh/")):
            raise BuildError(f"Unapproved/duplicate native path: {rel}")
        seen.add(rel.casefold())
        if not record.get("license") or not record.get("source_url") or record.get("redistribution_permitted") is not True:
            raise BuildError(f"Redistribution permission and provenance required for {rel}")
        url = urlsplit(record["source_url"])
        sensitive_query = {"token", "access_token", "key", "api_key", "sig", "signature", "password", "secret"}
        if url.scheme != "https" or not url.netloc or url.username or url.password or any(k.lower() in sensitive_query or k.lower().startswith("x-amz-") for k in parse_qs(url.query)):
            raise BuildError(f"Use a public HTTPS provenance URL without credentials or signed tokens for {rel}")
        if sha256(safe_file(source, rel)) != record.get("sha256"):
            raise BuildError(f"Native tool hash mismatch: {rel}")
        license_path = record.get("license_path", "")
        license_file = safe_file(source, license_path)
        if forbidden(license_path) or sha256(license_file) != record.get("license_sha256"):
            raise BuildError(f"Native licence hash mismatch: {rel}")
    return records


def stage_approved_tools(records, source, bundle):
    for i, record in enumerate(records):
        target = bundle / "Tools" / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_file(source, record["path"]), target)
        if sha256(target) != record["sha256"]:
            raise BuildError(f"Native tool changed during build: {record['path']}")
        license_dir = bundle / "licenses" / "native"
        license_dir.mkdir(parents=True, exist_ok=True)
        licence_target = license_dir / f"{i}-LICENSE.txt"
        shutil.copy2(safe_file(source, record["license_path"]), licence_target)
        if sha256(licence_target) != record["license_sha256"]:
            raise BuildError(f"Native licence changed during build: {record['path']}")


def audit_bundle(bundle):
    for path in bundle.rglob("*"):
        rel = path.relative_to(bundle).as_posix()
        if path.is_symlink() or path.is_junction() or rel.split("/")[0] not in TOP_LEVEL or forbidden(rel):
            raise BuildError(f"Forbidden/unexpected packaged path: {rel}")
    for required in ("Atelier.exe", "_internal/python312.dll", "_internal/version", "LICENSE", "README.txt", "BUILD_INFO.json", "NATIVE_TOOLS.json"):
        safe_file(bundle, required)
    for package in UNUSED_PACKAGES:
        if (bundle / "_internal" / package).exists():
            raise BuildError(f"Unused dependency included: {package}")


def normalized_code(code):
    return code.replace(co_filename="<source>", co_consts=tuple(
        normalized_code(c) if isinstance(c, types.CodeType) else c for c in code.co_consts))


def verify_pe_version(exe, version):
    import pefile
    expected = tuple(int(part) for part in version.split('.'))
    expected += (0,) * (4 - len(expected))
    with pefile.PE(str(exe)) as pe:
        if not getattr(pe, 'VS_FIXEDFILEINFO', None):
            raise BuildError('Executable is missing Windows version metadata.')
        fixed = pe.VS_FIXEDFILEINFO[0]
        for prefix in ('FileVersion', 'ProductVersion'):
            high, low = getattr(fixed, prefix + 'MS'), getattr(fixed, prefix + 'LS')
            if (high >> 16, high & 0xffff, low >> 16, low & 0xffff) != expected:
                raise BuildError(f'Executable {prefix} does not match {version}.')
        entries = {}
        for group in getattr(pe, 'FileInfo', []):
            for item in group:
                for table in getattr(item, 'StringTable', []):
                    entries.update(table.entries)
        for key in (b'FileVersion', b'ProductVersion'):
            if entries.get(key) != version.encode('ascii'):
                raise BuildError(f'Executable {key.decode()} string does not match {version}.')


def verify_resources(source, bundle):
    checks = []
    verify_pe_version(bundle / 'Atelier.exe', (source / 'version').read_text(encoding='utf-8').strip())
    for directory, pattern in (("gui", "*"), ("atelier/blender", "*.py")):
        for file in (source / directory).rglob(pattern):
            if file.is_file():
                rel = file.relative_to(source).as_posix()
                target = safe_file(bundle, "_internal/" + rel)
                if sha256(file) != sha256(target):
                    raise BuildError(f"Stale/missing resource: {rel}")
                checks.append(rel)
    if (bundle / "_internal/version").read_bytes() != (source / "version").read_bytes():
        raise BuildError("Frozen application version does not match source.")
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(bundle / "Atelier.exe"))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    for name in pyz.toc:
        if not (name == "io_lib" or name == "atelier" or name.startswith("atelier.")):
            continue
        rel = name.replace(".", "/")
        file = source / (rel + ".py")
        if not file.is_file():
            file = source / rel / "__init__.py"
        if not file.is_file():
            raise BuildError(f"Frozen Python module has no source: {name}")
        actual = pyz.extract(name)
        expected = compile(file.read_bytes(), "<source>", "exec", dont_inherit=True)
        if normalized_code(actual) != normalized_code(expected):
            raise BuildError(f"Frozen Python module differs from source: {name}")
        checks.append(name)
    actual = marshal.loads(archive.extract("window"))
    expected = compile((source / "window.py").read_bytes(), "<source>", "exec", dont_inherit=True)
    if normalized_code(actual) != normalized_code(expected):
        raise BuildError("Frozen launcher differs from source.")
    return checks


def file_records(root):
    return [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in sorted(root.rglob("*")) if p.is_file()]


def verify_payload(bundle, manifest):
    audit_bundle(bundle)
    if json.loads(safe_file(bundle, "MANIFEST.json").read_text(encoding="utf-8")) != manifest:
        raise BuildError("Packaged manifest changed after validation.")
    actual = [r for r in file_records(bundle) if r["path"] != "MANIFEST.json"]
    if actual != manifest["files"]:
        raise BuildError("Application payload changed after validation (modified, added or removed files).")


def verify_source_zip(path, records):
    with zipfile.ZipFile(path) as archive:
        expected = {"Atelier-source/" + r["path"] for r in records} | {"Atelier-source/SOURCE_MANIFEST.json"}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise BuildError("Source archive inventory does not match the validated snapshot.")
        if json.loads(archive.read("Atelier-source/SOURCE_MANIFEST.json")) != records:
            raise BuildError("Source archive manifest differs from the validated snapshot.")
        for record in records:
            data = archive.read("Atelier-source/" + record["path"])
            if forbidden(record["path"], source=True) or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise BuildError(f"Invalid source archive member: {record['path']}")


def extract_verified_portable(path, destination, manifest):
    expected = {"Atelier/" + r["path"] for r in manifest["files"]} | {"Atelier/MANIFEST.json"}
    with zipfile.ZipFile(path) as archive:
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise BuildError("Portable ZIP inventory does not match the validated payload.")
        # Paths are exact names from the audited manifest; verify content before extraction.
        for record in manifest["files"]:
            data = archive.read("Atelier/" + record["path"])
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise BuildError(f"Portable ZIP hash mismatch: {record['path']}")
        if json.loads(archive.read("Atelier/MANIFEST.json")) != manifest:
            raise BuildError("Portable ZIP has an inconsistent manifest.")
        destination.mkdir(parents=True, exist_ok=False)
        archive.extractall(destination)
    bundle = destination / "Atelier"
    verify_payload(bundle, manifest)
    return bundle


def verify_snapshot(root, expected):
    for file in root.rglob("*"):
        if file.is_file():
            safe_file(root, file.relative_to(root).as_posix())
        elif file.is_symlink() or file.is_junction():
            raise BuildError(f"Source snapshot acquired a link: {file}")
    actual = [r for r in file_records(root) if r["path"] != "SOURCE_MANIFEST.json"]
    if actual != expected:
        raise BuildError("Build/test tools modified the source snapshot; no matching source archive can be emitted.")


def write_installer_files(bundle, target):
    lines = []
    for record in file_records(bundle):
        rel = PurePosixPath(record["path"])
        dest = "{app}" + ("\\" + str(rel.parent).replace("/", "\\") if str(rel.parent) != "." else "")
        # Escape Inno literal braces and quotes; reject line breaks in filenames.
        source = str(bundle / record["path"])
        if any(c in source for c in '\r\n"'):
            raise BuildError(f"Unsafe installer filename: {rel}")
        source = source.replace("{", "{{")
        dest = dest[:5] + dest[5:].replace("{", "{{")
        flags = "onlyifdoesntexist uninsneveruninstall" if rel.name == "MarvelRivalsCharacterIDs.md" else "ignoreversion"
        lines.append(f'Source: "{source}"; DestDir: "{dest}"; Flags: {flags}')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def zip_tree(root, destination, prefix):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for record in file_records(root):
            archive.write(root / record["path"], prefix + record["path"])
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():
            raise BuildError(f"Corrupt archive: {destination}")


def zip_logs(root, destination):
    """Diagnostics only: never archive test-installed apps, profiles or old vendor binaries."""
    denied = {"isolated-data", "old-payload", "installed", "fresh-installed"}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(root.rglob("*")):
            rel = file.relative_to(root)
            if file.is_file() and not (set(rel.parts) & denied) and file.suffix.lower() in {".log", ".json", ".png"}:
                archive.write(file, "logs/" + rel.as_posix())
