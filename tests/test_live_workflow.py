"""Default runtime must work without indexed dependencies or persistent storage."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def test_default_runtime_has_no_index_imports_or_state(tmp_path):
    code = '''
import importlib.abc, sys
class NoIndex(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'storage', 'app', 'auth', 'analytics', 'chromadb', 'sentence_transformers', 'torch', 'numpy'}:
            raise AssertionError('Indexed import: ' + fullname)
sys.meta_path.insert(0, NoIndex())
from entrypoints.local.live import main
sys.argv = ['github-pr-context-mcp', 'config']
main()
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', HOME=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                            text=True, capture_output=True, check=True)
    config = json.loads(result.stdout)['mcpServers']['github']
    assert config['url'] == 'https://api.githubcopilot.com/mcp/'
    assert config['headers']['X-MCP-Readonly'] == 'true'
    assert list(tmp_path.iterdir()) == []


def test_packaging_defaults_and_explicit_legacy():
    import pytest
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    assert config['dependencies'] == ['mcp>=1.27.2,<2.0']
    assert config['scripts']['github-pr-context-mcp'] == 'entrypoints.local.live:main'
    assert config['scripts']['github-pr-context-indexed'] == 'entrypoints.local.server:main'
    assert 'chromadb==0.5.0' in config['optional-dependencies']['indexed']


def test_live_stdio_prompt_and_resource_without_storage(tmp_path):
    async def run():
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', HOME=str(tmp_path))
        params = StdioServerParameters(command=sys.executable,
                                      args=['-m', 'entrypoints.local.live'], env=env, cwd=str(tmp_path))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert tools == {'learning_status', 'record_learning', 'get_lessons', 'list_alerts',
                                 'acknowledge_alert', 'remove_lesson'}
                prompts = await session.list_prompts()
                assert {p.name for p in prompts.prompts} == {
                    'find_pr_context', 'learn_from_prs', 'check_lessons_before_change'}
                learn = await session.get_prompt('learn_from_prs', {'repo': 'example/project'})
                assert 'record_learning' in learn.messages[0].content.text
                assert 'example/project' in learn.messages[0].content.text
                prompt = await session.get_prompt('find_pr_context', {'repo': 'example/project', 'task': 'retry rationale'})
                text = prompt.messages[0].content.text
                assert 'example/project' in text and 'get_review_comments' in text
                resource = await session.read_resource('pr-context://workflow')
                assert 'local PR database, embeddings' in resource.contents[0].text
                assert 'Learning loop' in resource.contents[0].text
                assert 'untrusted data' in resource.contents[0].text
        assert list(tmp_path.iterdir()) == []
    asyncio.run(run())


def test_install_skill_refuses_overwrite(tmp_path):
    from entrypoints.local.live import main
    from unittest.mock import patch
    import pytest

    argv = ['github-pr-context-mcp', 'install-skill', '--skill-dir', str(tmp_path)]
    with patch.object(sys, 'argv', argv):
        main()
        skill = tmp_path / 'github-pr-context' / 'SKILL.md'
        original = skill.read_text()
        with pytest.raises(SystemExit):
            main()
        assert skill.read_text() == original
