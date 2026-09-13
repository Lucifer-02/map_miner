import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

import map_miner


def test_version_constant():
    """Kiểm tra hằng số __version__ trong map_miner và __all__."""
    assert hasattr(map_miner, "__version__")
    assert isinstance(map_miner.__version__, str)
    assert re.match(r"^\d+\.\d+\.\d+", map_miner.__version__)
    assert map_miner.__version__ == "0.2.3"
    assert "__version__" in map_miner.__all__


def test_pyproject_dynamic_versioning():
    """Kiểm tra pyproject.toml cấu hình Dynamic Versioning thông qua Hatchling."""
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert pyproject_path.exists(), f"pyproject.toml not found at {pyproject_path}"

    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    project_config = pyproject.get("project", {})
    assert "version" not in project_config or project_config.get("version") is None
    assert "version" in project_config.get("dynamic", [])

    hatch_version_config = pyproject.get("tool", {}).get("hatch", {}).get("version", {})
    assert hatch_version_config.get("path") == "src/map_miner/__init__.py"


def test_package_metadata_version():
    """Kiểm tra version trong metadata khớp với __version__ khi package đã cài đặt."""
    try:
        dist_version = importlib.metadata.version("map-miner")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Package 'map-miner' is not installed in metadata environment.")

    assert dist_version == map_miner.__version__
