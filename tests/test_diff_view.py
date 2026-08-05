"""
The DiffView seam: the interface generate_html renders through, and the two
adapters that satisfy it.

The first half needs no repository at all — a hand-built DiffView is a
complete input to the renderers, which is the leverage the seam buys. The
second half exercises the adapters, where the git invocations now live.
"""


# ─── Rendering from a hand-built view (no repo, no git) ─────────────

def test_diff_section_renders_from_hand_built_view(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="HEAD~3..HEAD",
        empty_label="HEAD~3..HEAD",
        sections=[("2 files changed, +9 −4", "<div>DIFF-BODY</div>")],
    ))
    assert "🔍 Diff Viewer — HEAD~3..HEAD" in out
    assert "2 files changed, +9 −4" in out
    assert "DIFF-BODY" in out


def test_diff_section_divides_consecutive_sections(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="Word-Level Change Detection",
        sections=[("Staged Changes (1 file)", "<div>A</div>"),
                  ("Unstaged Changes (2 files)", "<div>B</div>")],
    ))
    # One divider between the two sections — never leading or trailing.
    assert out.count('<div class="diff-divider"></div>') == 1
    assert out.index("<div>A</div>") < out.index("<div>B</div>")


def test_diff_section_single_section_has_no_divider(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="Word-Level Change Detection",
        sections=[("Staged Changes (1 file)", "<div>A</div>")],
    ))
    assert "diff-divider" not in out


def test_empty_view_shows_its_own_message(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="Word-Level Change Detection",
        empty_message="No uncommitted changes to diff",
    ))
    assert "No uncommitted changes to diff" in out
    # Working-tree mode leaves empty_label blank: no dash in the header.
    assert "🔍 Diff Viewer</div>" in out


def test_empty_view_keeps_a_label_when_it_has_one(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="v1..v2", empty_label="v1..v2",
        empty_message="No changes in this range",
    ))
    assert "🔍 Diff Viewer — v1..v2" in out
    assert "No changes in this range" in out


def test_diff_section_escapes_the_label(gd):
    out = gd._render_diff_section(gd.DiffView(
        label="<script>", sections=[("s", "<div>A</div>")]))
    assert "&lt;script&gt;" in out


def test_stats_row_renders_view_cards_then_stashes(gd):
    out = gd._render_stats_row(
        gd.RepoSnapshot(stash_count=2),
        gd.DiffView(stat_cards=[
            ("Files Changed", "modified", 3, "3"),
            ("Insertions", "staged", 9, "+9"),
            ("Deletions", "untracked", 0, "−0"),
        ]),
    )
    assert out.index("Files Changed") < out.index("Insertions") < out.index("Stashes")
    assert '<div class="stat-value">+9</div>' in out
    # A zero count is dimmed, whatever its display text.
    assert '<div class="stat-value zero">−0</div>' in out
    assert '<div class="stat-value">2</div>' in out


def test_stats_row_dims_a_zero_stash_count(gd):
    out = gd._render_stats_row(gd.RepoSnapshot(stash_count=0), gd.DiffView())
    assert '<div class="stat-value zero">0</div>' in out


# ─── WorkingTreeDiffs adapter ───────────────────────────────────────

def test_working_tree_view_has_both_sections(gd, repo, git):
    (repo / "a.txt").write_text("changed line\n")     # modified, unstaged
    (repo / "b.txt").write_text("staged line\n")
    git("add", "b.txt", cwd=repo)                      # staged

    snap = gd.RepoSnapshot.from_repo(str(repo))
    view = gd.WorkingTreeDiffs(str(repo), snap)

    labels = [label for label, _ in view.sections]
    assert labels == ["Staged Changes (1 file)", "Unstaged Changes (1 file)"]
    assert view.label == "Word-Level Change Detection"
    assert view.empty_label == ""
    assert [c[0] for c in view.stat_cards] == ["Staged", "Modified", "Untracked"]


def test_clean_working_tree_view_is_empty(gd, repo):
    snap = gd.RepoSnapshot.from_repo(str(repo))
    view = gd.WorkingTreeDiffs(str(repo), snap)

    assert view.sections == []
    assert view.empty_message == "No uncommitted changes to diff"


# ─── RangeDiffs adapter ─────────────────────────────────────────────

def test_two_dot_range_labels_itself_verbatim(gd, repo, commit):
    commit(repo, "a.txt", "line one\nline two\n", "second commit")
    view = gd.RangeDiffs(str(repo), "HEAD~1..HEAD")

    assert view.label == "HEAD~1..HEAD"
    assert view.empty_label == "HEAD~1..HEAD"
    assert [label for label, _ in view.sections] == ["1 file changed, +1 −0"]
    assert view.stat_cards[1] == ("Insertions", "staged", 1, "+1")


def test_bare_ref_range_labels_the_working_tree(gd, repo):
    (repo / "a.txt").write_text("line one\nline two\n")
    view = gd.RangeDiffs(str(repo), "HEAD")

    assert view.label == "HEAD..working tree"


def test_empty_range_reports_no_changes(gd, repo):
    view = gd.RangeDiffs(str(repo), "HEAD..HEAD")

    assert view.sections == []
    assert view.empty_message == "No changes in this range"
    assert view.stat_cards[0] == ("Files Changed", "modified", 0, "0")


# ─── build_diff_view: the one place mode is decided ─────────────────

def test_build_diff_view_picks_the_adapter(gd, repo):
    snap = gd.RepoSnapshot.from_repo(str(repo))
    assert isinstance(gd.build_diff_view(str(repo), None, snap),
                      gd.WorkingTreeDiffs)
    assert isinstance(gd.build_diff_view(str(repo), "HEAD~1..HEAD", snap),
                      gd.RangeDiffs)


def test_generate_html_runs_no_git(gd, repo, monkeypatch):
    """The renderer is pure: every git call happens in the adapter, before
    generate_html is ever entered."""
    snap = gd.RepoSnapshot.from_repo(str(repo))
    view = gd.build_diff_view(str(repo), None, snap)

    def _fail(args, cwd):
        raise AssertionError(f"generate_html invoked git: {args}")

    monkeypatch.setattr(gd, "run_git", _fail)
    assert "<html" in gd.generate_html(snap, str(repo), view)
