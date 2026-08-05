"""
Golden-master snapshot tests for generate_html (Phase 2, Task B).

These pin the exact HTML output for three fixed repo scenarios, as the
safety net for a future "extract the HTML into templates" refactor: after
the refactor, these tests prove the output is byte-identical.

RE-BLESSING (after an *intentional* output change):

    UPDATE_GOLDENS=1 script/test -k golden

then review the diff of tests/golden/*.html before committing. Never
re-bless to silence a failure you can't explain.

Determinism — three non-deterministic sources are neutralized:

1. git's %ar relative dates ("6 years ago" drifts with the wall clock, and
   Python-side clock freezing can't reach the git subprocess): every
   relative-date field in the info dict is overwritten with a fixed string
   before rendering (_scrub_relative_dates).
2. datetime.now() inside generate_html (the "Generated ..." header stamp):
   the module's datetime class is monkeypatched to a frozen one.
3. The absolute repo path (generate_html embeds str(repo_path) in the
   "Path:" header line, and pytest's tmp_path differs every run — a third
   source beyond the two the brief listed; see FINDINGS.md): the path is
   replaced with a {{REPO_PATH}} placeholder in the output before
   comparing. This is a targeted substitution, not DOM normalization —
   the comparison is still exact-string.

Commit SHAs in the output are already reproducible: the conftest git
environment pins author/committer identity and dates, and each scenario
builds identical commits every run. (They do depend on git's SHA-1 and
default abbreviation length staying stable; if goldens ever mismatch on
hashes alone after a git upgrade, re-bless and review.)
"""

import os
from datetime import datetime
from html import escape
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"

# Fixed stand-in for git's wall-clock-relative %ar dates.
FIXED_RELATIVE_DATE = "some time ago"


class _FrozenDatetime(datetime):
    """A datetime whose now() is pinned to a constant instant."""

    @classmethod
    def now(cls, tz=None):
        return cls(2020, 6, 15, 12, 0, 0)


@pytest.fixture
def frozen_clock(gd, monkeypatch):
    """Pin the 'Generated ...' timestamp generate_html stamps into the HTML."""
    monkeypatch.setattr(gd, "datetime", _FrozenDatetime)


def _scrub_relative_dates(info):
    """Overwrite every %ar-derived field with a fixed string.

    last_date isn't currently rendered by generate_html, but it is scrubbed
    anyway so the info dict holds nothing clock-relative.
    """
    info["last_date"] = FIXED_RELATIVE_DATE
    for c in info["recent_commits"]:
        c["date"] = FIXED_RELATIVE_DATE
    for c in info["main_recent_commits"]:
        c["date"] = FIXED_RELATIVE_DATE
    return info


def _render(gd, repo):
    """collect + scrub + render + placeholder the per-run repo path."""
    info = _scrub_relative_dates(gd.collect_repo_info(str(repo)))
    view = gd.build_diff_view(str(repo), None, info)
    html_out = gd.generate_html(info, str(repo), view)
    return html_out.replace(escape(str(repo)), "{{REPO_PATH}}")


def _assert_matches_golden(name, html_out):
    golden_path = GOLDEN_DIR / name
    if os.environ.get("UPDATE_GOLDENS"):
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden_path.write_text(html_out)
    assert golden_path.exists(), (
        f"golden file {golden_path} is missing — generate it with:"
        f" UPDATE_GOLDENS=1 script/test -k golden"
    )
    assert html_out == golden_path.read_text(), (
        f"generate_html output no longer matches {golden_path.name}. If the"
        f" change is intentional, re-bless with UPDATE_GOLDENS=1 and review"
        f" the golden diff; if not, this is the safety net firing."
    )


# ─── Scenario 1: clean repo ─────────────────────────────────────────

def test_golden_clean_repo(gd, git, commit, tmp_path, frozen_clock):
    repo = tmp_path / "golden-clean"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    commit(repo, "a.txt", "line one\n", "initial commit")

    _assert_matches_golden("clean.html", _render(gd, repo))


# ─── Scenario 2: staged + modified + untracked, diffs rendered ──────

def test_golden_dirty_repo(gd, git, commit, tmp_path, frozen_clock):
    repo = tmp_path / "golden-dirty"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    commit(repo, "a.txt", "hello world\n", "initial commit")
    commit(repo, "b.txt", "value = 1\n", "add b.txt")

    (repo / "a.txt").write_text("hello brave world\n")   # modified, unstaged
    (repo / "b.txt").write_text("value = 2\n")           # modified + staged
    git("add", "b.txt", cwd=repo)
    (repo / "c.txt").write_text("new file\n")            # untracked

    _assert_matches_golden("dirty.html", _render(gd, repo))


# ─── Scenario 3: feature branch, bottom "main" card shown ───────────

def test_golden_feature_branch_with_main_card(gd, git, commit, tmp_path,
                                              frozen_clock):
    repo = tmp_path / "golden-feature"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    commit(repo, "base.txt", "base\n", "commit A")
    commit(repo, "main2.txt", "on main\n", "commit B (main only)")
    # Branch from one commit back so main has a commit the feature branch
    # lacks: show_main_card is true and the card shows "behind main" state.
    git("checkout", "-q", "-b", "feature", "main~1", cwd=repo)
    commit(repo, "feat.txt", "on feature\n", "commit C (feature only)")

    _assert_matches_golden("feature_branch.html", _render(gd, repo))
