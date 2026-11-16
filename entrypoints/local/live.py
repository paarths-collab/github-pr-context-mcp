"""Storage-free workflow companion for the host's official GitHub MCP connection."""

import argparse
import json
from pathlib import Path
import shutil
import sysconfig

from mcp.server.fastmcp import FastMCP


SKILL_NAME = "github-pr-context"
GITHUB_CONFIG = {
    "url": "https://api.githubcopilot.com/mcp/",
    "headers": {"X-MCP-Readonly": "true", "X-MCP-Toolsets": "repos,pull_requests"},
}


def skill_directory():
    source = Path(__file__).resolve().parents[2] / ".agents" / "skills" / SKILL_NAME
    if source.is_dir():
        return source
    installed = Path(sysconfig.get_path("data")) / "share" / "github-pr-context-mcp" / "skills" / SKILL_NAME
    if installed.is_dir():
        return installed
    raise FileNotFoundError("Packaged live retrieval skill is missing; reinstall github-pr-context-mcp.")


def workflow_text():
    return (skill_directory() / "SKILL.md").read_text(encoding="utf-8")


mcp = FastMCP(
    "GitHub PR Context",
    instructions=("Workflow companion only. Connect the official GitHub MCP separately to search and read PRs. "
                  "This server has no GitHub API tools, local index, or embeddings."),
)


@mcp.resource("pr-context://workflow", mime_type="text/markdown")
def live_workflow() -> str:
    """Live PR retrieval workflow for the host agent."""
    return workflow_text()


@mcp.prompt()
def find_pr_context(repo: str, task: str) -> str:
    """Guide the host agent through live GitHub search and PR review reads."""
    # JSON quotes keep user inputs separate from workflow instructions.
    return workflow_text() + "\n\nUser request data:\n" + json.dumps({"repo": repo, "task": task})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["config", "install-skill"])
    parser.add_argument("--skill-dir", type=Path, default=Path(".agents/skills"))
    args = parser.parse_args()
    if args.command == "config":
        print(json.dumps({"mcpServers": {"github": GITHUB_CONFIG}}, indent=2))
        return
    if args.command == "install-skill":
        target = args.skill_dir / SKILL_NAME
        if target.exists():
            parser.error(f"Skill already exists: {target}")
        shutil.copytree(skill_directory(), target)
        print(f"Installed {target}")
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
