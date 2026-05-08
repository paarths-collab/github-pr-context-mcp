import pytest
from unittest.mock import patch, MagicMock
import subprocess
from app.state import detect_repo_from_git

def test_detect_repo_from_git_https():
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.check_output") as mock_output, \
         patch("os.path.exists", return_value=True):
        
        mock_output.return_value = b"https://github.com/paarths-collab/github-pr-context-mcp.git"
        
        repo = detect_repo_from_git("/fake/path")
        assert repo == "paarths-collab/github-pr-context-mcp"

def test_detect_repo_from_git_ssh():
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.check_output") as mock_output, \
         patch("os.path.exists", return_value=True):
        
        mock_output.return_value = b"git@github.com:paarths-collab/github-pr-context-mcp.git"
        
        repo = detect_repo_from_git("/fake/path")
        assert repo == "paarths-collab/github-pr-context-mcp"

def test_detect_repo_from_git_no_remote():
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.check_output") as mock_output, \
         patch("os.path.exists", return_value=True):
        
        mock_output.side_effect = subprocess.CalledProcessError(1, "git")
        
        repo = detect_repo_from_git("/fake/path")
        assert repo is None
