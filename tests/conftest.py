"""
Shared fixtures for the git-dashboard test suite.

Two jobs:

1. Load the module under test. The filename has a hyphen
   (``git-dashboard.py``), so it cannot be imported by name — it is loaded
   via importlib as the session-scoped ``gd`` fixture. Loading is
   side-effect-free: the module only defines functions/classes/constants,
   and ``main()`` runs only under ``if __name__ == "__main__"``.

2. Build hermetic, deterministic git repositories. Every test runs with the
   sandbox's global/system git config masked, a fixed author/committer
   identity, and fixed dates. "Remote" scenarios use a local bare repository
   in tmp_path — nothing ever touches the network (snapshotting a repo
   runs ``git fetch``, so a real remote URL would hang the suite).

Test setup drives git via subprocess directly (``_git``), never through the
module's own ``run_git`` — that function is itself under test.
"""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "git-dashboard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("git_dashboard", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def gd():
    """The git-dashboard module under test."""
    return _load_module()


# ─── Hermetic git environment ───────────────────────────────────────

@pytest.fixture(autouse=True)
def git_env(monkeypatch, tmp_path):
    """
    Isolate every test from the sandbox's git configuration, and pin
    identity/dates so repository state is deterministic.

    GIT_TERMINAL_PROMPT=0 is a tripwire: if a test ever reaches a remote
    that wants credentials, git fails immediately instead of hanging.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "git-config-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-01-01T00:00:00 +0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00 +0000")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


# ─── Repo-building helpers ──────────────────────────────────────────

def _git(*args, cwd):
    """Run git for test setup. Raises on failure (unlike the module's run_git)."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(repo, filename, content, message=None):
    """Write a file (creating parent dirs), stage it, and commit."""
    path = Path(repo) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git("add", "--", filename, cwd=repo)
    _git("commit", "-q", "-m", message or f"commit {filename}", cwd=repo)


@pytest.fixture
def git(git_env):
    """The _git setup helper, with the hermetic environment applied."""
    return _git


@pytest.fixture
def commit(git_env):
    """The _commit setup helper, with the hermetic environment applied."""
    return _commit


@pytest.fixture
def make_repo(tmp_path, git_env):
    """Factory: fresh repo with one commit (a.txt) and no remote."""
    def _make(name="repo", branch="main"):
        repo = tmp_path / name
        repo.mkdir()
        _git("init", "-q", "-b", branch, cwd=repo)
        _commit(repo, "a.txt", "line one\n", "initial commit")
        return repo
    return _make


@pytest.fixture
def repo(make_repo):
    """A fresh repo on main with one commit (a.txt) and no remote."""
    return make_repo()


@pytest.fixture
def origin_setup(tmp_path, git_env):
    """
    Offline "remote" rig:

      .bare   local bare repository acting as origin (one commit on main)
      .repo   clone A — the repo under test; main tracks origin/main,
              origin/HEAD is set (clone does this), starts in sync
      .other  clone B — a "collaborator" used to push commits to origin
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _commit(seed, "a.txt", "line one\n", "initial commit")

    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)

    repo = tmp_path / "clone_a"
    _git("clone", "-q", str(bare), str(repo), cwd=tmp_path)
    other = tmp_path / "clone_b"
    _git("clone", "-q", str(bare), str(other), cwd=tmp_path)

    return SimpleNamespace(bare=bare, repo=repo, other=other)
