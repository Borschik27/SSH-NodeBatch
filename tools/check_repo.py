"""Check public files and packaged defaults without printing sensitive contents."""
from pathlib import Path
import re
import subprocess
import sys

import yaml

from release_files import DOCUMENTS, resource_files

PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
PERSONAL_HOME = re.compile(rb"(?:/home/[A-Za-z0-9_.-]+/|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+[\\/])")
GENERATED = {"build", "dist", ".local", ".work", ".test-temp", "__pycache__", ".pytest_cache"}
FORBIDDEN_SUFFIXES = {".sqlite3", ".db", ".jsonl", ".pem", ".key", ".p12", ".pfx", ".log", ".exe", ".dll", ".so", ".whl", ".zip"}


def public_files(root):
    if (root / ".git").exists():
        result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}",
            "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=True)
        return [Path(name) for name in result.stdout.decode("utf-8").split("\0") if name]
    paths = [p.relative_to(root) for name in ("src", "tests", "tools", "docs")
             for p in (root / name).rglob("*") if p.is_file() and not GENERATED.intersection(p.parts)
             and not any(part.endswith(".egg-info") for part in p.relative_to(root).parts)]
    return paths + [Path(name) for name in DOCUMENTS if (root / name).is_file()]


def check(root):
    root = Path(root).resolve()
    errors = []
    declared_data = {source.relative_to(root) for source, _ in resource_files(root)}
    candidates = public_files(root)
    for missing in declared_data - set(candidates):
        errors.append(f"Packaged resource is hidden from the public source tree: {missing}")
    for relative in candidates:
        path = root / relative
        if GENERATED.intersection(relative.parts) or path.suffix in FORBIDDEN_SUFFIXES:
            errors.append(f"Generated or private file is a Git candidate: {relative}")
            continue
        allowed_suffixes = {"tools": {".py"}, "tests": {".py"}, "docs": {".md"}, ".github": {".yml", ".yaml"},
                            "licenses": {".txt"}}
        if relative.parts[0] in allowed_suffixes and path.suffix not in allowed_suffixes[relative.parts[0]]:
            errors.append(f"Unexpected generated/data file in public source directory: {relative}")
            continue
        if relative.parts[0] == "src" and relative.parts[:2] != ("src", "nodebatch"):
            errors.append(f"Unexpected source or generated metadata directory: {relative}")
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            errors.append(f"Public files must not link outside the source tree: {relative}")
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if relative.parts[:2] == ("src", "nodebatch") and path.suffix != ".py" and relative not in declared_data:
            errors.append(f"Undeclared package data: {relative}")
        if path.suffix == ".py" and re.search("[\u0400-\u04ff]", data.decode("utf-8")):
            errors.append(f"Keep Python source in English; use locale catalogs for translations: {relative}")
        if PRIVATE_KEY.search(data):
            errors.append(f"Private key material detected: {relative}")
        if PERSONAL_HOME.search(data):
            errors.append(f"Machine-specific home path detected: {relative}")
        if any(dash.encode() in data for dash in ("\u2013", "\u2014")):
            errors.append(f"Use an ordinary hyphen: {relative}")
    package = root / "src/nodebatch/defaults"
    settings = yaml.safe_load((package / "settings.yaml").read_text())
    for profile in settings["profiles"].values():
        if any(profile.get(key) for key in ("config", "known_hosts", "keys", "wsl_home", "wsl_distribution")):
            errors.append("Packaged profiles must not contain operator-specific paths, keys or distributions")
    inventory = yaml.safe_load((package / "inventory.yaml").read_text())
    if inventory.get("hosts") or inventory.get("groups"):
        errors.append("Packaged inventory must be empty")
    resource_files(root)
    for name in DOCUMENTS:
        if not (root / name).is_file():
            errors.append(f"Missing public document: {name}")
    return errors


if __name__ == "__main__":
    errors = check(Path(__file__).resolve().parents[1])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("OK: public files and release defaults contain no local artifacts")
