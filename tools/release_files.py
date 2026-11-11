"""Explicit data and documentation manifests for reproducible application builds."""
from pathlib import Path
import tomllib

DOCUMENTS = (
    "README.md", "README.ru.md", "CHANGELOG.md", "LICENSE",
    "docs/configuration.md", "docs/configuration.ru.md",
    "docs/execution.md", "docs/execution.ru.md",
    "docs/development.md", "docs/development.ru.md",
    "THIRD_PARTY.md",
    "licenses/GPL-3.0.txt", "licenses/LGPL-3.0.txt",
    "licenses/Python.txt", "licenses/PyYAML.txt",
    "licenses/PyInstaller.txt",
)


def resource_files(root):
    root = Path(root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    names = project["tool"]["setuptools"]["package-data"]["nodebatch"]
    package = root / "src" / "nodebatch"
    result = []
    for name in names:
        relative = Path(name)
        path = package / relative
        if (relative.is_absolute() or ".." in relative.parts or any(c in name for c in "*?[]")
                or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(package.resolve())):
            raise ValueError(f"Invalid release resource: {name}")
        result.append((path, Path("nodebatch") / relative.parent))
    return result
