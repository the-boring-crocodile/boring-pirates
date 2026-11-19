"""The built release requires the Crocodile admin inventory contract."""
import ast
import json
import runpy
import sys
from types import ModuleType
from pathlib import Path

import boring_deploy
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version,accepted", [
    ("0.5.0rc7", False), ("0.5.0rc12", False), ("0.5.0rc13", False), ("0.5.0rc14", False),
    ("0.5.0rc21", False), ("0.5.0", True),
    ("0.5.0", True),
    ("0.5.1", True), ("0.6.0", False),
])
def test_built_release_requires_admin_inventory(tmp_path, monkeypatch, version, accepted):
    # Read the builder's actual metadata; keep this policy out of Common's engine.
    tree = ast.parse((ROOT / "scripts/build.py").read_text())
    release_dict, = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "release" for t in node.targets)]
    # Import the actual builder declarations without loading a wheel or building.
    loader = ModuleType("_develop")
    loader.load = lambda: (None, None)
    monkeypatch.setitem(sys.modules, "_develop", loader)
    namespace = runpy.run_path(str(ROOT / "scripts/build.py"))
    requirement_node, = [value for key, value in zip(release_dict.keys, release_dict.values)
                         if isinstance(key, ast.Constant) and key.value == "requires"]
    requires = eval(compile(ast.Expression(requirement_node), "release.requires", "eval"), namespace)
    croc = tmp_path / "crocodile"
    (croc / "current").mkdir(parents=True)
    (croc / "current/VERSION").write_text(version + "\n")
    homes = tmp_path / "homes.json"
    homes.write_text(json.dumps({"boring-crocodile": str(croc)}))
    monkeypatch.setattr(boring_deploy, "HOMES", homes)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "RELEASE.json").write_text(json.dumps(dict(
        component="boring-pirates", version="1.0.0", runtime="venv",
        default_home=str(tmp_path / "pirates"), requires=requires)))
    release = boring_deploy.Release.load(artifact)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    if accepted:
        boring_deploy.check_requires(release)
    else:
        with pytest.raises(boring_deploy.Failure, match="needs boring-crocodile"):
            boring_deploy.check_requires(release)
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
