"""
Group B: the watch-mode modules — DashboardCache (the rendered dashboard
and its staleness rule) and ClientRegistry (SSE fan-out) — driven entirely
in-process. No HTTP server, no poll thread, no sleeps: every refresh
decision poll_loop and the /refresh handler can make is asserted here as a
plain method call against a real tmp repo.
"""

import os


# ─── DashboardCache: the poll-tick rule ─────────────────────────────

def test_first_refresh_builds_the_dashboard(gd, repo):
    cache = gd.DashboardCache(str(repo), None)
    assert cache.get_html() == b""

    assert cache.refresh_if_changed() is True
    html = cache.get_html()
    assert b"a.txt" not in html  # clean repo: no changed files listed
    assert b"/events" in html    # watch script was injected


def test_unchanged_repo_is_a_quiet_tick(gd, repo):
    cache = gd.DashboardCache(str(repo), None)
    cache.refresh_if_changed()
    html = cache.get_html()

    assert cache.refresh_if_changed() is False
    # Not just equal bytes — the very same object, proving no rebuild ran
    # (a rebuild always assigns a fresh bytes object).
    assert cache.get_html() is html


def test_repo_change_triggers_rebuild(gd, repo):
    cache = gd.DashboardCache(str(repo), None)
    cache.refresh_if_changed()

    (repo / "a.txt").write_text("line two\n")
    assert cache.refresh_if_changed() is True
    html = cache.get_html()
    assert b"a.txt" in html  # the modified file now appears in the page
    assert b"two" in html    # and the diff shows the edited word

    # And the tick after that is quiet again.
    assert cache.refresh_if_changed() is False


def test_force_refresh_rebuilds_unconditionally(gd, repo):
    cache = gd.DashboardCache(str(repo), None)
    cache.refresh_if_changed()
    html = cache.get_html()

    cache.force_refresh()
    assert cache.get_html() is not html  # rebuilt despite an unchanged repo


def test_force_refresh_leaves_no_spurious_second_refresh(gd, origin_setup,
                                                         commit, git):
    # ⭐ The ordering rule inside force_refresh: the hash is recomputed
    # *after* regenerating, because regenerating runs `git fetch`. Build the
    # scenario where the order matters: a collaborator pushes, so the
    # force_refresh's own fetch moves origin/main. A pre-fetch hash would
    # mismatch on the next tick and cause a spurious second refresh.
    cache = gd.DashboardCache(str(origin_setup.repo), None)
    cache.refresh_if_changed()

    commit(origin_setup.other, "remote.txt", "remote\n", "remote commit")
    git("push", "-q", "origin", "main", cwd=origin_setup.other)

    cache.force_refresh()
    assert b"behind" in cache.get_html()  # the fetch really moved the ref

    assert cache.refresh_if_changed() is False  # next poll tick is quiet


# ─── DashboardCache: the /open allowlist ────────────────────────────

def test_allowlist_tracks_changed_files(gd, repo):
    cache = gd.DashboardCache(str(repo), None)
    cache.refresh_if_changed()

    root = os.path.realpath(str(repo))
    a_txt = os.path.realpath(str(repo / "a.txt"))
    assert cache.is_allowed(root)        # repo root is always allowed
    assert not cache.is_allowed(a_txt)   # clean tracked file is not

    (repo / "a.txt").write_text("edited\n")
    cache.refresh_if_changed()
    assert cache.is_allowed(a_txt)       # now modified → now allowed
    assert not cache.is_allowed(os.path.realpath("/etc/hosts"))


# ─── ClientRegistry: fan-out and shutdown ───────────────────────────

def test_broadcast_reaches_every_client(gd):
    clients = gd.ClientRegistry()
    q1 = clients.add_client()
    q2 = clients.add_client()

    clients.broadcast("refresh")
    assert q1.get_nowait() == "refresh"
    assert q2.get_nowait() == "refresh"


def test_removed_client_stops_receiving(gd):
    clients = gd.ClientRegistry()
    q1 = clients.add_client()
    q2 = clients.add_client()
    clients.remove_client(q2)

    clients.broadcast("refresh")
    assert q1.get_nowait() == "refresh"
    assert q2.empty()


def test_shutdown_wakes_every_client(gd):
    clients = gd.ClientRegistry()
    q1 = clients.add_client()
    q2 = clients.add_client()

    assert not clients.shutdown_event.is_set()
    clients.shutdown()
    assert clients.shutdown_event.is_set()
    assert q1.get_nowait() == "__shutdown__"
    assert q2.get_nowait() == "__shutdown__"
