"""
Golden-master snapshot tests for generate_html (Phase 2, Task B).

These pin the exact HTML output for three fixed repo scenarios, as the
safety net for a future "extract the HTML into templates" refactor: after
the refactor, these tests prove the output is byte-identical.

RE-BLESSING (after an *intentional* output change):

    UPDATE_GOLDENS=1 script/test -k golden

then review the diff of tests/golden/*.html before committing. Never
re-bless to silence a failure you can't explain.

Determinism — three non-deterministic sources are neutralized, the first
two by deriving a pinned snapshot with dataclasses.replace (RepoSnapshot is
frozen, and both facts are its named fields — no monkeypatching):

1. git's %ar relative dates ("6 years ago" drifts with the wall clock, and
   Python-side clock freezing can't reach the git subprocess): every Commit
   is replaced with one carrying a fixed date (_pin_snapshot).
2. The "Generated ..." header stamp: generate_html reads it from
   snap.generated_at rather than the clock, so _pin_snapshot fixes it too.
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
from dataclasses import replace
from datetime import datetime
from html import escape
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"

# Fixed stand-in for git's wall-clock-relative %ar dates.
FIXED_RELATIVE_DATE = "some time ago"

# Fixed instant for the "Generated ..." header stamp.
FIXED_INSTANT = datetime(2020, 6, 15, 12, 0, 0)


def _pin_snapshot(snap):
    """Derive a snapshot with every clock-relative fact pinned: the render
    timestamp, and each commit's %ar date."""
    def pin_dates(commits):
        return [replace(c, date=FIXED_RELATIVE_DATE) for c in commits]

    return replace(
        snap,
        generated_at=FIXED_INSTANT,
        recent_commits=pin_dates(snap.recent_commits),
        main_recent_commits=pin_dates(snap.main_recent_commits),
    )


def _render(gd, repo):
    """snapshot + pin + render + placeholder the per-run repo path."""
    snap = _pin_snapshot(gd.RepoSnapshot.from_repo(str(repo)))
    view = gd.build_diff_view(str(repo), None, snap)
    html_out = gd.generate_html(snap, str(repo), view)
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

def test_golden_clean_repo(gd, git, commit, tmp_path):
    repo = tmp_path / "golden-clean"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    commit(repo, "a.txt", "line one\n", "initial commit")

    _assert_matches_golden("clean.html", _render(gd, repo))


# ─── Scenario 2: staged + modified + untracked, diffs rendered ──────

def test_golden_dirty_repo(gd, git, commit, tmp_path):
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

def test_golden_feature_branch_with_main_card(gd, git, commit, tmp_path):
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
