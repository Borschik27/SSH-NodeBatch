"""Build on each target OS: python tools/build.py (no cross compilation)."""
import os
import argparse
import subprocess
import sys
import uuid
from pathlib import Path
from check_repo import check
from release_files import DOCUMENTS, resource_files

parser = argparse.ArgumentParser()
parser.add_argument("--clean", action="store_true", help="Rebuild dependency analysis from scratch")
parser.add_argument("--dist-root", type=Path, help="Write verification builds separately from existing dist")
options = parser.parse_args()
root = Path(__file__).resolve().parents[1]
os.chdir(root)
errors = check(root)
if errors:
    raise SystemExit("\n".join(errors))
data_options = []
for source, destination in resource_files(root):
    data_options.extend(["--add-data", str(source) + os.pathsep + destination.as_posix()])
target = (options.dist_root or root / "dist").resolve() / ("windows" if os.name == "nt" else "linux")
target.mkdir(parents=True, exist_ok=True)
staging = target / (".stage-" + uuid.uuid4().hex)
staging.mkdir()
if not staging.resolve().is_relative_to(target.resolve()):
    raise SystemExit("Build staging directory escapes the requested output root")
build_env = os.environ.copy()
if os.name == "nt":
    # Do not package unrelated DLLs from tools such as Poppler on the caller's
    # PATH (its ICU DLL can have the same filename and an incompatible ABI).
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    build_env["PATH"] = os.pathsep.join(map(str, [
        Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows]))
common = [sys.executable, "-m", "PyInstaller", "--noconfirm", *(["--clean"] if options.clean else []),
          "--paths", str(root / "src"), "--specpath", str(root / "build" / target.name / "spec"),
          "--workpath", str(root / "build" / target.name / "work")]
subprocess.run([*common, "--name", "NodeBatch", "--onedir", "--windowed",
                *data_options, "--distpath", str(staging),
                str(root / "tools/main_entry.py")], check=True, env=build_env)
subprocess.run([*common, "--name", "nodebatch-askpass", "--onedir", "--console",
                "--distpath", str(staging / "NodeBatch"),
                str(root / "tools/askpass_entry.py")], check=True, env=build_env)
import shutil
for document in DOCUMENTS:
    destination = staging / "NodeBatch" / document
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / document, destination)
bundle = staging / "NodeBatch"
destination = target / "NodeBatch"
if (not bundle.resolve().is_relative_to(staging.resolve()) or destination.is_symlink()
        or not destination.resolve().is_relative_to(target.resolve())):
    raise SystemExit("Unsafe bundle publication path")
if destination.exists():
    backup = target / (".previous-" + uuid.uuid4().hex)
    if not backup.resolve().is_relative_to(target.resolve()):
        raise SystemExit("Unsafe previous-bundle path")
    destination.rename(backup)
bundle.rename(destination)
staging.rmdir()
print("Built:", target / "NodeBatch")
