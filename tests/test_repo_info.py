"""
Group B: run_git, find_default_ref, and collect_repo_info against real
(hermetic, offline) git repositories built in tmp_path.
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


# ─── collect_repo_info ──────────────────────────────────────────────

def test_collect_basic_fields_no_remote(gd, repo, commit):
    commit(repo, "second.txt", "two\n", "second commit")
    info = gd.collect_repo_info(str(repo))

    assert info["repo_name"] == repo.name
    assert info["branch"] == "main"
    assert info["last_msg"] == "second commit"
    # Characterization: total_commits stays a string (raw rev-list output).
    assert info["total_commits"] == "2"
    assert info["remote_url"] == "No remote"
    assert info["tracking"] == ""
    assert info["sync_status"] == "No remote tracking"
    assert (info["ahead"], info["behind"]) == (0, 0)
    assert info["staged_files"] == []
    assert info["modified_files"] == []
    assert info["untracked_files"] == []
    assert info["stash_count"] == 0
    assert info["github_url"] == ""
    assert [c["msg"] for c in info["recent_commits"]] == [
        "second commit",
        "initial commit",
    ]


def test_collect_file_buckets(gd, repo, git):
    (repo / "a.txt").write_text("modified content\n")  # tracked, unstaged edit
    (repo / "b.txt").write_text("staged content\n")
    git("add", "b.txt", cwd=repo)
    (repo / "c.txt").write_text("untracked content\n")

    info = gd.collect_repo_info(str(repo))
    assert info["staged_files"] == ["b.txt"]
    assert info["modified_files"] == ["a.txt"]
    assert info["untracked_files"] == ["c.txt"]


def test_collect_untracked_dir_lists_files_individually(gd, repo):
    # ls-files --others enumerates files inside untracked directories
    # (unlike default porcelain status, which collapses to "d/").
    d = repo / "d"
    d.mkdir()
    (d / "a.txt").write_text("one\n")
    (d / "b.txt").write_text("two\n")

    info = gd.collect_repo_info(str(repo))
    assert "d/a.txt" in info["untracked_files"]
    assert "d/b.txt" in info["untracked_files"]


def test_collect_stash_count(gd, repo, git):
    (repo / "a.txt").write_text("stash me\n")
    git("stash", cwd=repo)
    info = gd.collect_repo_info(str(repo))
    assert info["stash_count"] == 1


def test_collect_sync_in_sync(gd, origin_setup):
    info = gd.collect_repo_info(str(origin_setup.repo))
    assert info["tracking"] == "origin/main"
    assert (info["ahead"], info["behind"]) == (0, 0)
    assert info["sync_status"] == "In sync"
    assert info["unpushed_hashes"] == set()
    # Local filesystem remote — no github_url derived.
    assert info["github_url"] == ""


def test_collect_sync_ahead_after_local_commit(gd, origin_setup, commit):
    commit(origin_setup.repo, "local.txt", "local\n", "local-only commit")
    info = gd.collect_repo_info(str(origin_setup.repo))
    assert (info["ahead"], info["behind"]) == (1, 0)
    assert info["sync_status"] == "Ahead"
    assert len(info["unpushed_hashes"]) == 1


def test_collect_sync_behind_after_remote_commit(gd, origin_setup, commit, git):
    # A collaborator (clone B) pushes; collect_repo_info fetches internally,
    # so clone A sees itself behind without any explicit local fetch.
    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    info = gd.collect_repo_info(str(origin_setup.repo))
    assert (info["ahead"], info["behind"]) == (0, 1)
    assert info["sync_status"] == "Behind"


def test_collect_sync_diverged(gd, origin_setup, commit, git):
    commit(origin_setup.repo, "local.txt", "local\n", "local commit")
    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    info = gd.collect_repo_info(str(origin_setup.repo))
    assert (info["ahead"], info["behind"]) == (1, 1)
    assert info["sync_status"] == "Diverged"
