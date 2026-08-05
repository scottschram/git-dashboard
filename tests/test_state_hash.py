"""
Group B: compute_state_hash — the poll loop's change-detection fingerprint —
plus the end-to-end "remote change shows up" scenario.

compute_state_hash never fetches; it folds together porcelain status
(--untracked-files=all), HEAD, @{upstream}, the default-branch head, and the
unstaged + staged diffs. Each test isolates one component moving.
"""


def test_hash_is_deterministic(gd, repo):
    assert gd.compute_state_hash(str(repo)) == gd.compute_state_hash(str(repo))


def test_hash_changes_on_each_edit_to_same_file(gd, repo):
    # Porcelain status alone only flags *that* a.txt is modified — it looks
    # identical after the first and second edit. The folded-in `git diff`
    # output is what distinguishes successive edits; assert it does.
    h_clean = gd.compute_state_hash(str(repo))
    (repo / "a.txt").write_text("first edit\n")
    h_first = gd.compute_state_hash(str(repo))
    (repo / "a.txt").write_text("second edit\n")
    h_second = gd.compute_state_hash(str(repo))

    assert h_clean != h_first
    assert h_first != h_second
    assert h_clean != h_second


def test_hash_changes_for_new_file_in_untracked_directory(gd, repo):
    # ⭐ Regression guard for commit aa3ac23 (--untracked-files=all; see the
    # compute_state_hash docstring, git-dashboard.py lines 1303-1307).
    # Default porcelain status collapses an untracked directory to a single
    # "d/" entry, so a *second* file appearing inside the already-untracked
    # directory would not change the status text and the poll loop would
    # never refresh. --untracked-files=all lists each file, so it must.
    h_clean = gd.compute_state_hash(str(repo))

    d = repo / "d"
    d.mkdir()
    (d / "a.txt").write_text("one\n")
    h_one_file = gd.compute_state_hash(str(repo))

    (d / "b.txt").write_text("two\n")
    h_two_files = gd.compute_state_hash(str(repo))

    assert h_clean != h_one_file
    assert h_one_file != h_two_files


def test_hash_survives_non_utf8_diff(gd, repo, git):
    # ⭐ Regression guard: a real-world PDF had no NUL byte in the first 8000
    # bytes (git's binary sniff window), so `git diff` rendered its deletion
    # as *text* — including 0xE2 from the `%âãÏÓ` header line, which is
    # invalid UTF-8. run_git decoded stdout strictly, so the
    # UnicodeDecodeError escaped subprocess.run and crashed the dashboard at
    # startup. Synthetic stand-in: a PDF-ish header with those bytes, NUL-free.
    doc = repo / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4\r%\xe2\xe3\xcf\xd3\r\n"
                    + b"synthetic pdf-ish body line\n" * 4)
    git("add", "doc.pdf", cwd=repo)
    git("commit", "-q", "-m", "add pdf-ish doc", cwd=repo)
    h_clean = gd.compute_state_hash(str(repo))
    doc.unlink()  # unstaged deletion, as in the original crash

    # Must not raise; undecodable bytes come back as U+FFFD.
    diff = gd.run_git(["diff"], str(repo))
    assert "doc.pdf" in diff
    assert "�" in diff

    # And run_git must not mangle content: subprocess *text mode* applies
    # universal-newline translation, turning every embedded \r into \n —
    # which inflated one 11,567-line binary diff to 23,980 lines. The \r
    # inside the header line must survive as \r.
    assert "%PDF-1.4\r" in diff

    # And the deletion still registers as a state change.
    assert gd.compute_state_hash(str(repo)) != h_clean


def test_hash_changes_when_edit_is_staged(gd, repo, git):
    # Staging moves the same content from the unstaged-diff component to the
    # staged-diff component; the hash must move with it.
    (repo / "a.txt").write_text("edited\n")
    h_unstaged = gd.compute_state_hash(str(repo))
    git("add", "a.txt", cwd=repo)
    h_staged = gd.compute_state_hash(str(repo))
    assert h_unstaged != h_staged


def test_hash_changes_when_upstream_moves(gd, origin_setup, commit, git):
    h_before = gd.compute_state_hash(str(origin_setup.repo))

    # A collaborator pushes to origin. compute_state_hash never fetches, so
    # until clone A fetches, nothing it hashes has changed.
    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)
    assert gd.compute_state_hash(str(origin_setup.repo)) == h_before

    # After a fetch, origin/main (= @{upstream} here) has moved: new hash.
    git("fetch", "-q", "origin", cwd=origin_setup.repo)
    assert gd.compute_state_hash(str(origin_setup.repo)) != h_before


def test_hash_changes_when_default_branch_moves(gd, origin_setup, commit, git):
    # On a feature branch with no upstream, a fetch that moves origin/main
    # changes nothing except the default-branch component: status, HEAD,
    # @{upstream} (empty), and both diffs are identical before/after.
    git("checkout", "-q", "-b", "feature", cwd=origin_setup.repo)
    h_before = gd.compute_state_hash(str(origin_setup.repo))

    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)
    git("fetch", "-q", "origin", cwd=origin_setup.repo)

    assert gd.compute_state_hash(str(origin_setup.repo)) != h_before


def test_remote_change_shows_up_end_to_end(gd, origin_setup, commit, git):
    # The motivating scenario: work appears "in another session" (clone B
    # pushes to origin). The dashboard must notice: the snapshot's own fetch
    # flips the branch panel to "behind", and the state hash — the poll
    # loop's refresh trigger — changes.
    def branch_panel(snap):
        return gd._render_branch_panel(snap, gd._render_commits(snap, ""))

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))
    assert (snap.ahead, snap.behind) == (0, 0)
    assert "In sync" in branch_panel(snap)
    h_before = gd.compute_state_hash(str(origin_setup.repo))

    commit(origin_setup.other, "collab.txt", "from another session\n",
           "collaborator commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    snap = gd.RepoSnapshot.from_repo(str(origin_setup.repo))  # fetches internally
    assert snap.behind >= 1
    assert "↓ 1 behind" in branch_panel(snap)
    assert gd.compute_state_hash(str(origin_setup.repo)) != h_before
