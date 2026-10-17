"""The Claude Code plugin is wired correctly: manifests parse and point at real files, the launcher
is valid shell, every skill has frontmatter, and every tool a skill names exists on the server."""

import asyncio
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "rightsize"


def test_marketplace_points_at_the_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "rightsize")
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


def test_plugin_manifest_and_mcp_config():
    from rightsize_core import __version__

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "rightsize" and manifest["version"] == __version__
    server = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["rightsize"]
    script = server["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    assert Path(script).exists()
    for sh in (PLUGIN / "bin").iterdir():
        assert subprocess.run(["sh", "-n", str(sh)], capture_output=True, check=False).returncode == 0, sh.name


def test_skills_and_commands_have_frontmatter():
    docs = list((PLUGIN / "skills").glob("*/SKILL.md")) + list((PLUGIN / "commands").glob("*.md"))
    assert len(docs) >= 3
    for doc in docs:
        head = doc.read_text().split("---")[1]
        name = re.search(r"^name:\s*(\S+)", head, re.MULTILINE).group(1)
        assert name == (doc.parent.name if doc.name == "SKILL.md" else doc.stem), doc
        assert re.search(r"^description:", head, re.MULTILINE), doc


def test_every_tool_the_plugin_names_exists():
    from rightsize_core.mcp_server import build_server

    tools = {t.name for t in asyncio.run(build_server(ROOT).list_tools())}
    texts = [p.read_text() for p in PLUGIN.rglob("*.md")]
    named = {m for t in texts for m in re.findall(r"`([a-z_]+)\(", t)}
    readme = (PLUGIN / "README.md").read_text()
    listed = set(re.findall(r"`([a-z_]+)`", readme.split("**Tools**")[1].split("**Command:**")[0]))
    assert named and named <= tools, named - tools
    assert listed - {"rightsize"} == tools  # the server's own name is in the sentence too
