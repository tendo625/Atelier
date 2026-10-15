"""Collect actual installed Python distribution and interpreter licence texts."""
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys

from windows_package import BuildError, pinned_requirements


def collect_licenses(root, destination):
    destination.mkdir(parents=True, exist_ok=True)
    names = set(pinned_requirements(root / "requirements.txt")) | {"setuptools", "pyinstaller"}
    records = []
    for name in sorted(names):
        distribution = importlib.metadata.distribution(name)
        copied = []
        for entry in distribution.files or []:
            leaf_notice = entry.name.lower().startswith(("license", "copying", "notice"))
            in_notice_dir = any(part.lower() in {"licenses", "licences"} for part in entry.parts[:-1])
            text_file = entry.suffix.lower() in {"", ".txt", ".md", ".rst"}
            if "__pycache__" in entry.parts or not (leaf_notice or (in_notice_dir and text_file)):
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            parts = [p for p in entry.parts if p not in {"..", ".", ""}]
            target = destination / f"{name}-{distribution.version}" / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.relative_to(destination).as_posix())
        supplemental = root / "licenses" / name
        if supplemental.is_dir():
            for source in supplemental.iterdir():
                if source.is_file():
                    target = destination / f"{name}-{distribution.version}" / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(target.relative_to(destination).as_posix())
        if not copied:
            raise BuildError(f"No licence text found for installed distribution {name}.")
        records.append({"name": name, "version": distribution.version, "files": copied})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise BuildError(f"Python licence missing: {python_license}")
    shutil.copy2(python_license, destination / "PYTHON-LICENSE.txt")
    (destination / "runtime-packages.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records
