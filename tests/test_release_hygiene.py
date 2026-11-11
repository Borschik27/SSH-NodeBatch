from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_repo import check
from release_files import DOCUMENTS, resource_files


@pytest.fixture
def source_tree(tmp_path):
    shutil.copy2(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for source, _ in resource_files(ROOT):
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in DOCUMENTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Public documentation\n")
    return tmp_path


def test_real_repository_is_generic_and_clean():
    assert not check(ROOT)


def test_source_archive_ignores_generated_distribution_metadata(source_tree):
    metadata = source_tree / "src/ssh_nodebatch.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("Generated distribution metadata\n")
    assert not check(source_tree)


def test_private_data_is_not_a_release_resource(source_tree):
    private = source_tree / "src/nodebatch/operator-data.json"
    private.write_text('{"host": "private-example"}')
    assert private not in {source for source, _ in resource_files(source_tree)}
    assert any("Undeclared package data" in error for error in check(source_tree))
    private.unlink()
    local = source_tree / ".local/reports"
    local.mkdir(parents=True)
    (local / "runtime.jsonl").write_text("local-only\n")
    assert not check(source_tree)


def test_private_material_is_reported_without_echoing_it(source_tree):
    marker = "-----BEGIN" + " PRIVATE KEY-----"
    (source_tree / "README.md").write_text(marker + "\nsynthetic\n")
    errors = check(source_tree)
    assert any("Private key material detected" in error for error in errors)
    assert all(marker not in error and "synthetic" not in error for error in errors)
