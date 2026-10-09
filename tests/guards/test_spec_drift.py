import json
from pathlib import Path

from rightsize_core.spec_export import schemas

ROOT = Path(__file__).resolve().parents[2]


def test_published_schemas_match_code():
    for name, schema in schemas().items():
        path = ROOT / "spec" / "schemas" / name
        assert path.exists(), f"missing {path}; run `rightsize spec`"
        assert json.loads(path.read_text()) == json.loads(json.dumps(schema)), f"{name} drifted; run `rightsize spec`"
