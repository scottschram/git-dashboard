"""
Group B: run_git, find_default_ref, and RepoSnapshot against real
(hermetic, offline) git repositories built in tmp_path.

Sync state has no field of its own — the snapshot carries the facts
(tracking, tracking_gone, ahead, behind) and the branch panel turns them
into a badge — so the sync tests assert both: the facts, and the badge the
dashboard actually shows.
"""

import subprocess


# ─── run_git ────────────────────────────────────────────────────────

def test_run_git_returns_stripped_stdout(gd, repo):
    assert gd.run_git(["branch", "--show-current"], str(repo)) == "main"
    assert gd.run_git(["log", "-1", "--format=%s"], repo) == "initial commit"


def test_run_git_bogus_subcommand_returns_empty(gd, repo):
    # Characterization: run_git never checks the exit code — a failing git
    # command (here: unknown subcommand, non-zero exit, output on stderr)
    # just yields its empty stdout, indistinguishable from real empty output.
    assert gd.run_git(["definitely-not-a-git-subcommand"], str(repo)) == ""


def test_run_git_failed_command_returns_empty(gd, repo):
    assert gd.run_git(
        ["rev-parse", "--verify", "--quiet", "no-such-ref"], str(repo)
    ) == ""


def test_run_git_missing_git_binary_returns_empty(gd, repo, monkeypatch, tmp_path):
    # The FileNotFoundError branch: point PATH at an empty directory so the
    # git executable cannot be found.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert gd.run_git(["status"], str(repo)) == ""


def test_run_git_timeout_returns_empty(gd, repo, monkeypatch):
    # The TimeoutExpired branch, without waiting 30s: substitute a
    # subprocess.run that raises it.
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(gd.subprocess, "run", fake_run)
    assert gd.run_git(["status"], str(repo)) == ""


# ─── find_default_ref ───────────────────────────────────────────────

def test_default_ref_prefers_origin_main(gd, origin_setup):
    # Cloning sets refs/remotes/origin/HEAD, so the name comes from the
    # remote and the ref prefers origin/main over local main.
    assert gd.find_default_ref(str(origin_setup.repo)) == ("main", "origin/main")


def test_default_ref_no_remote_falls_back_to_local_main(gd, repo):
    assert gd.find_default_ref(str(repo)) == ("main", "main")


def test_default_ref_master_repo(gd, make_repo):
    repo = make_repo(name="master-repo", branch="master")
    assert gd.find_default_ref(str(repo)) == ("master", "master")


def test_default_ref_empty_repo_nothing_detectable(gd, git, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git("init", "-q", "-b", "main", cwd=empty)
    # No commits: "main" exists as unborn HEAD only, so nothing verifies.
    assert gd.find_default_ref(str(empty)) == ("", "")


# ─── RepoSnapshot.from_repo ─────────────────────────────────────────

def _branch_panel(gd, snap):
    """The rendered top-right panel — where sync state reaches the user."""
    return gd._render_branch_panel(snap, gd._render_commits(snap, ""))


def test_snapshot_basic_fields_no_remote(gd, repo, commit):
    commit(repo, "second.txt", "two\n", "second commit")
    snap = gd.RepoSnapshot.from_repo(str(repo))

    assert snap.repo_name == repo.name
    assert snap.branch == "main"
    assert snap.remote_url == "No remote"
    assert snap.tracking == ""
    assert (snap.ahead, snap.behind) == (0, 0)
    assert snap.staged_files == []
    assert snap.modified_files == []
    assert snap.untracked_files == []
    assert snap.stash_count == 0
    assert snap.github_url == ""
    assert [c.msg for c in snap.recent_commits] == [
        "second commit",
        "initial commit",
    ]
    # No upstream configured — the badge says so.
    assert "No remote" in _branch_panel(gd, snap)


def test_snapshot_file_buckets(gd, repo, git):
    (repo / "a.txt").write_text("modified content\n")  # tracked, unstaged edit
    (repo / "b.txt").write_text("staged content\n")
    git("add", "b.txt", cwd=repo)
    (repo / "c.txt").write_text("untracked content\n")

    snap = gd.RepoSnapshot.from_repo(str(repo))
    assert snap.staged_files == ["b.txt"]
    assert snap.modified_files == ["a.txt"]
    assert snap.untracked_files == ["c.txt"]


def test_snapshot_untracked_dir_lists_files_individually(gd, repo):
    # ls-files --others enumerates files inside untracked directories
    # (unlike default porcelain status, which collapses to "d/").
    d = repo / "d"
    d.mkdir()
    (d / "a.txt").write_text("one\n")
    (d / "b.txt").write_text("two\n")

    snap = gd.RepoSnapshot.from_repo(str(repo))
    assert "d/a.txt" in snap.untracked_files
    assert "d/b.txt" in snap.untracked_files


def test_snapshot_stash_count(gd, repo, git):
    (repo / "a.txt").write_text("stash me\n")
    git("stash", cwd=repo)
    snap = gd.RepoSnapshot.from_repo(str(repo))
    assert snap.stash_count == 1


def test_snapshot_sync_in_sync(gd, origin_setup):
    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert snap.tracking == "origin/main"
    assert (snap.ahead, snap.behind) == (0, 0)
    assert snap.unpushed_hashes == set()
    assert "In sync" in _branch_panel(gd, snap)
    # Local filesystem remote — no github_url derived.
    assert snap.github_url == ""


def test_snapshot_sync_ahead_after_local_commit(gd, origin_setup, commit):
    commit(origin_setup.repo, "local.txt", "local\n", "local-only commit")
    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert (snap.ahead, snap.behind) == (1, 0)
    assert len(snap.unpushed_hashes) == 1
    assert "↑ 1 unpushed" in _branch_panel(gd, snap)


def test_snapshot_sync_behind_after_remote_commit(gd, origin_setup, commit, git):
    # A collaborator (clone B) pushes; from_repo fetches internally, so
    # clone A sees itself behind without any explicit local fetch.
    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert (snap.ahead, snap.behind) == (0, 1)
    assert "↓ 1 behind" in _branch_panel(gd, snap)


def test_snapshot_sync_diverged(gd, origin_setup, commit, git):
    commit(origin_setup.repo, "local.txt", "local\n", "local commit")
    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert (snap.ahead, snap.behind) == (1, 1)
    # Diverged shows both badges at once.
    panel = _branch_panel(gd, snap)
    assert "↑ 1 unpushed" in panel
    assert "↓ 1 behind" in panel


def _delete_upstream(origin_setup, git):
    """Put clone A in the 'upstream gone' state: branch config still names
    origin/main, but the ref no longer resolves (remote branch deleted,
    remote-tracking ref pruned). from_repo's internal fetch cannot restore
    it — the bare repo's branch is gone too."""
    git("update-ref", "-d", "refs/heads/main", cwd=origin_setup.bare)
    git("update-ref", "-d", "refs/remotes/origin/main", cwd=origin_setup.repo)


def test_snapshot_upstream_gone_is_flagged(gd, origin_setup, commit, git):
    # Regression: rev-list against a gone ref exits non-zero, run_git
    # returns "", and ahead/behind read as 0/0 — which used to render a
    # false green "In sync" over genuinely unpushed commits. tracking_gone
    # is the field that distinguishes the two.
    commit(origin_setup.repo, "local.txt", "local\n", "unpushed commit")
    _delete_upstream(origin_setup, git)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert snap.tracking == "origin/main"  # config survives the prune
    assert snap.tracking_gone is True
    assert (snap.ahead, snap.behind) == (0, 0)  # uncomputable, hence the flag


def test_snapshot_upstream_gone_marks_all_commits_unpushed(gd, origin_setup,
                                                           commit, git):
    # With the upstream ref gone, no local commit can be on it — the seed
    # commit plus the local one both count as unpushed.
    commit(origin_setup.repo, "local.txt", "local\n", "unpushed commit")
    _delete_upstream(origin_setup, git)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert len(snap.unpushed_hashes) == 2
    assert all(c.full_hash in snap.unpushed_hashes
               for c in snap.recent_commits)


def test_render_upstream_gone_badge(gd, origin_setup, commit, git):
    commit(origin_setup.repo, "local.txt", "local\n", "unpushed commit")
    _delete_upstream(origin_setup, git)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    panel = _branch_panel(gd, snap)
    assert "Upstream gone" in panel
    assert "In sync" not in panel


def test_snapshot_healthy_upstream_not_flagged_gone(gd, origin_setup):
    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert snap.tracking_gone is False
    assert "In sync" in _branch_panel(gd, snap)


# ─── Rendering from a literal snapshot ──────────────────────────────
#
# The point of the record: a panel can be rendered from facts stated
# outright, with no repository on disk and no git at all.

def test_literal_snapshot_renders_branch_panel(gd, monkeypatch):
    def _fail(args, cwd):
        raise AssertionError(f"rendering invoked git: {args}")

    monkeypatch.setattr(gd, "run_git", _fail)

    unpushed = gd.Commit(hash="cafe123", full_hash="cafe123deadbeef",
                         msg="not pushed yet", date="2 hours ago")
    pushed = gd.Commit(hash="beef456", full_hash="beef456cafebabe",
                       msg="already on the remote", date="3 days ago")
    snap = gd.RepoSnapshot(
        branch="feature",
        tracking="origin/feature",
        ahead=1,
        recent_commits=[unpushed, pushed],
        unpushed_hashes={unpushed.full_hash},
    )

    panel = _branch_panel(gd, snap)
    assert "↑ 1 unpushed" in panel
    assert "not pushed yet" in panel
    assert "commit-unpushed" in panel
    # No default branch resolved, so this is the plain commits panel.
    assert snap.show_main_card is False
    assert "📜 Recent Commits" in panel


def test_literal_snapshot_renders_main_panel(gd):
    behind = gd.Commit(hash="aaa1111", full_hash="aaa1111full",
                       msg="landed on main", date="1 day ago")
    snap = gd.RepoSnapshot(
        branch="feature",
        default_branch="main",
        default_ref="origin/main",
        main_recent_commits=[behind],
        behind_main_hashes={behind.full_hash},
    )

    assert snap.show_main_card is True
    assert snap.behind_main == 1
    panel = gd._render_main_panel(snap, "https://github.com/u/r")
    assert "↓ 1 behind main" in panel
    assert "commit-behind-main" in panel
    assert "https://github.com/u/r/commit/aaa1111full" in panel
