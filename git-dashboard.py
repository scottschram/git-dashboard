#!/usr/bin/env python3
"""
git-dashboard.py — Git status dashboard with word-level diff highlighting.

Generates an HTML dashboard showing repository status, file changes, and
critically: sub-line diffs that highlight exactly which words changed within
modified paragraphs. No more squinting at entire red/green paragraphs to
find the one curly quote that got straightened.

Usage:
    python3 git-dashboard.py [path-to-repo]                       # watch (default)
    python3 git-dashboard.py --once [path-to-repo]                # one-shot snapshot
    python3 git-dashboard.py --range HEAD~3..HEAD [path-to-repo]  # range (implies --once)
    python3 git-dashboard.py --range HEAD~3 [path-to-repo]        # range + working tree

Options:
    --range <spec>   Show diffs for a commit range instead of working tree.
                     Two-dot range (HEAD~3..HEAD) shows committed changes only.
                     Single ref (HEAD~3) or open-ended (HEAD~3..) includes
                     uncommitted working tree changes as well.
                     Implies --once (history can't be meaningfully watched).
    --once           One-shot mode: generate HTML, open browser, exit.
                     Useful for scripts and CI. Default is watch mode.
    --help, -h       Show this help message and exit.

Default behavior is watch mode: starts a local server, opens the browser,
auto-refreshes when the repo changes. Ctrl-C to stop.

Opens in default browser automatically on macOS.
"""

import subprocess
import sys
import os
import re
import webbrowser
import hashlib
import queue
import threading
import string
from datetime import datetime
from difflib import SequenceMatcher
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# Polling interval for watch mode (the default). Trades responsiveness for
# fewer git invocations; 5s is the sweet spot for a "glance while you code" tool.
POLL_INTERVAL_SECONDS = 5

# macOS app the Terminal launcher opens. Override with the environment:
#   export GIT_DASHBOARD_TERMINAL=iTerm
TERMINAL_APP = os.environ.get("GIT_DASHBOARD_TERMINAL", "Terminal")


# ─── Git Data Collection ────────────────────────────────────────────

def run_git(args, cwd):
    """Run a git command and return stdout."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def find_default_ref(repo_path):
    """
    Resolve the project's default branch.

    Returns (name, ref) — name is e.g. "main"/"master"/"trunk", ref is the
    actual git ref to compare against (preferring origin/{name} so we see
    collaborator pushes, falling back to local {name} so no-remote / scratch
    repos still get a working bottom card). Either may be "" if the repo has
    no detectable default branch.
    """
    name = ""
    sym = run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo_path)
    if sym.startswith("origin/"):
        name = sym[len("origin/"):]
    else:
        for candidate in ("main", "master"):
            if run_git(["rev-parse", "--verify", "--quiet", candidate], repo_path):
                name = candidate
                break

    ref = ""
    if name:
        if run_git(["rev-parse", "--verify", "--quiet", f"origin/{name}"], repo_path):
            ref = f"origin/{name}"
        elif run_git(["rev-parse", "--verify", "--quiet", name], repo_path):
            ref = name
    return name, ref


def derive_github_url(remote_url):
    """
    Derive the browsable GitHub web URL from a remote URL.

    Handles SSH (git@github.com:user/repo.git) and HTTPS
    (https://github.com/user/repo.git) forms, stripping a trailing .git.
    Returns "" for anything that isn't a GitHub remote.
    """
    if "github.com" not in remote_url:
        return ""
    gh = re.sub(r'^git@github\.com:', 'https://github.com/', remote_url)
    gh = re.sub(r'\.git$', '', gh)
    return gh


def collect_repo_info(repo_path):
    """Collect all repository metadata."""
    info = {}
    info["repo_name"] = os.path.basename(
        run_git(["rev-parse", "--show-toplevel"], repo_path) or repo_path
    )
    info["branch"] = run_git(["branch", "--show-current"], repo_path) or "detached"
    info["last_hash"] = run_git(["log", "-1", "--format=%h"], repo_path) or "none"
    info["last_msg"] = run_git(["log", "-1", "--format=%s"], repo_path) or "No commits yet"
    info["last_date"] = run_git(["log", "-1", "--format=%ar"], repo_path) or "never"
    info["last_author"] = run_git(["log", "-1", "--format=%an"], repo_path) or "unknown"
    info["total_commits"] = run_git(["rev-list", "--count", "HEAD"], repo_path) or "0"

    # Remote info
    info["remote_url"] = run_git(["remote", "get-url", "origin"], repo_path) or "No remote"
    info["tracking"] = run_git(
        ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{info['branch']}"],
        repo_path
    ) or ""

    # Fetch quietly for accurate ahead/behind
    run_git(["fetch", "--quiet"], repo_path)

    info["ahead"] = 0
    info["behind"] = 0
    info["sync_status"] = "No remote tracking"
    if info["tracking"]:
        ahead = run_git(["rev-list", "--count", f"{info['tracking']}..HEAD"], repo_path)
        behind = run_git(["rev-list", "--count", f"HEAD..{info['tracking']}"], repo_path)
        info["ahead"] = int(ahead) if ahead.isdigit() else 0
        info["behind"] = int(behind) if behind.isdigit() else 0
        a, b = info["ahead"], info["behind"]
        if a == 0 and b == 0:
            info["sync_status"] = "In sync"
        elif a > 0 and b > 0:
            info["sync_status"] = "Diverged"
        elif a > 0:
            info["sync_status"] = "Ahead"
        else:
            info["sync_status"] = "Behind"

    # Default branch info — used by the bottom "main" card on feature
    # branches. ref may be origin/main or local main; see find_default_ref.
    default_name, default_ref = find_default_ref(repo_path)
    info["default_branch"] = default_name
    info["default_ref"] = default_ref
    info["show_main_card"] = bool(default_ref and default_name != info["branch"])

    info["main_recent_commits"] = []
    info["behind_main_hashes"] = set()
    if info["show_main_card"]:
        behind = run_git(["rev-list", f"HEAD..{default_ref}"], repo_path)
        if behind:
            info["behind_main_hashes"] = set(behind.splitlines())
        main_log = run_git(
            ["log", "-12", "--format=%h\t%H\t%s\t%ar", default_ref], repo_path
        )
        # Main isn't the focus on a feature branch — show all the "new on main"
        # commits we can fit (up to 12), but cap "already in my branch" context
        # at 6 entries. If there are >12 behind commits, the badge still shows
        # the true count even though the list is capped.
        IN_SYNC_CAP = 6
        in_sync_seen = 0
        for line in (main_log.splitlines() if main_log else []):
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            is_behind = parts[1] in info["behind_main_hashes"]
            if not is_behind:
                in_sync_seen += 1
                if in_sync_seen > IN_SYNC_CAP:
                    break
            info["main_recent_commits"].append({
                "hash": parts[0], "full_hash": parts[1],
                "msg": parts[2], "date": parts[3],
            })
    info["behind_main"] = len(info["behind_main_hashes"])

    # Stash count
    stash = run_git(["stash", "list"], repo_path)
    info["stash_count"] = len(stash.splitlines()) if stash else 0

    # File counts
    staged = run_git(["diff", "--cached", "--name-only"], repo_path)
    modified = run_git(["diff", "--name-only"], repo_path)
    untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo_path)
    info["staged_files"] = staged.splitlines() if staged else []
    info["modified_files"] = modified.splitlines() if modified else []
    info["untracked_files"] = untracked.splitlines() if untracked else []

    # Detailed status with flags
    info["staged_status"] = run_git(["diff", "--cached", "--name-status"], repo_path)
    info["modified_status"] = run_git(["diff", "--name-status"], repo_path)

    # Unpushed commit hashes (full hashes for reliable comparison)
    info["unpushed_hashes"] = set()
    if info["tracking"]:
        unpushed = run_git(["rev-list", f"{info['tracking']}..HEAD"], repo_path)
        if unpushed:
            info["unpushed_hashes"] = set(unpushed.splitlines())

    # Recent commits (include full hash for GitHub links). On a feature branch
    # we only show commits unique to the branch — anything already on main
    # belongs in the bottom "main" card, not duplicated here.
    if info["show_main_card"]:
        log = run_git(
            ["log", "-12", "--format=%h\t%H\t%s\t%ar", f"{default_ref}..HEAD"],
            repo_path,
        )
    else:
        log = run_git(["log", "--oneline", "-12", "--format=%h\t%H\t%s\t%ar"], repo_path)
    info["recent_commits"] = []
    for line in (log.splitlines() if log else []):
        parts = line.split("\t", 3)
        if len(parts) == 4:
            info["recent_commits"].append({
                "hash": parts[0], "full_hash": parts[1],
                "msg": parts[2], "date": parts[3]
            })

    # Derive GitHub web URL from remote
    info["github_url"] = derive_github_url(info["remote_url"])

    return info


# ─── Word-Level Diff Engine ─────────────────────────────────────────

def tokenize_for_diff(text):
    """
    Split text into tokens preserving whitespace and punctuation as separate tokens.
    This gives us fine-grained diffing — we can see individual quote changes,
    word substitutions, and punctuation swaps.
    """
    # Split into words, whitespace, and punctuation clusters
    tokens = re.findall(r'\S+|\s+', text)
    # Further split punctuation from words for even finer granularity
    result = []
    for token in tokens:
        if token.isspace():
            result.append(token)
        else:
            # Split punctuation away from word boundaries
            # e.g., '"Hello,' -> ['"', 'Hello', ',']
            parts = re.findall(r'[^\w\s]+|[\w]+', token)
            result.extend(parts)
    return result


def word_level_diff(old_line, new_line):
    """
    Compare two lines at word level and return HTML with changed words highlighted.
    Returns (old_html, new_html) with <mark> tags around changes.
    """
    old_tokens = tokenize_for_diff(old_line)
    new_tokens = tokenize_for_diff(new_line)

    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    old_html_parts = []
    new_html_parts = []

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk = escape("".join(old_tokens[i1:i2]))
        new_chunk = escape("".join(new_tokens[j1:j2]))

        if op == "equal":
            old_html_parts.append(old_chunk)
            new_html_parts.append(new_chunk)
        elif op == "replace":
            old_html_parts.append(f'<mark class="wdel">{old_chunk}</mark>')
            new_html_parts.append(f'<mark class="wadd">{new_chunk}</mark>')
        elif op == "delete":
            old_html_parts.append(f'<mark class="wdel">{old_chunk}</mark>')
        elif op == "insert":
            new_html_parts.append(f'<mark class="wadd">{new_chunk}</mark>')

    return "".join(old_html_parts), "".join(new_html_parts)


def compute_similarity(old_line, new_line):
    """How similar are two lines? 0.0 = totally different, 1.0 = identical."""
    # IMPORTANT: disable SequenceMatcher's autojunk heuristic.
    #
    # For long lines (common in Markdown prose), autojunk=True can treat very
    # frequent characters as "junk" and drastically under-estimate similarity.
    # That breaks our removed→added line pairing, which in turn prevents
    # word-level highlighting from being applied.
    return SequenceMatcher(None, old_line, new_line, autojunk=False).ratio()


def pair_changed_lines(removed_lines, added_lines):
    """
    Match removed lines to added lines by similarity.
    Returns list of (old_line_or_None, new_line_or_None) pairs.
    
    This is the key insight: when git shows 5 red lines then 5 green lines,
    we need to figure out which red maps to which green for word-level comparison.
    """
    if not removed_lines and not added_lines:
        return []

    if not removed_lines:
        return [(None, line) for line in added_lines]
    if not added_lines:
        return [(line, None) for line in removed_lines]

    # Build similarity matrix
    pairs = []
    used_new = set()
    used_old = set()

    # First pass: find strong matches (similarity > 0.4)
    scores = []
    for i, old in enumerate(removed_lines):
        for j, new in enumerate(added_lines):
            sim = compute_similarity(old, new)
            if sim > 0.4:
                scores.append((sim, i, j))

    # Greedy matching by highest similarity
    scores.sort(reverse=True)
    matched_pairs = {}
    for sim, i, j in scores:
        if i not in used_old and j not in used_new:
            matched_pairs[i] = j
            used_old.add(i)
            used_new.add(j)

    # Build output: interleave matched pairs and unmatched lines
    old_idx = 0
    new_idx = 0

    # Process in order
    for i in range(len(removed_lines)):
        if i in matched_pairs:
            # Emit any unmatched new lines that come before matched new line
            j = matched_pairs[i]
            while new_idx < j:
                if new_idx not in used_new:
                    pairs.append((None, added_lines[new_idx]))
                new_idx += 1
            pairs.append((removed_lines[i], added_lines[j]))
            new_idx = j + 1
        else:
            pairs.append((removed_lines[i], None))

    # Remaining unmatched new lines
    while new_idx < len(added_lines):
        pairs.append((None, added_lines[new_idx]))
        new_idx += 1

    return pairs


# ─── Diff Parser ────────────────────────────────────────────────────

def parse_and_render_diff(diff_text, diff_label):
    """
    Parse git diff output and render as HTML with word-level highlighting.
    """
    if not diff_text:
        return ""

    html_parts = []
    current_file = None
    hunk_removed = []  # Buffer for consecutive removed lines
    hunk_added = []    # Buffer for consecutive added lines
    in_hunk = False

    def flush_change_buffer():
        """Process buffered removed/added lines with word-level diff."""
        nonlocal hunk_removed, hunk_added
        if not hunk_removed and not hunk_added:
            return

        pairs = pair_changed_lines(hunk_removed, hunk_added)

        for old_line, new_line in pairs:
            if old_line is not None and new_line is not None:
                # Matched pair — do word-level diff
                old_html, new_html = word_level_diff(old_line, new_line)
                html_parts.append(
                    f'<div class="diff-line diff-del">-{old_html}</div>'
                )
                html_parts.append(
                    f'<div class="diff-line diff-add">+{new_html}</div>'
                )
            elif old_line is not None:
                # Pure deletion
                html_parts.append(
                    f'<div class="diff-line diff-del">-{escape(old_line)}</div>'
                )
            else:
                # Pure addition
                html_parts.append(
                    f'<div class="diff-line diff-add">+{escape(new_line)}</div>'
                )

        hunk_removed = []
        hunk_added = []

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git"):
            flush_change_buffer()
            if current_file:
                html_parts.append("</div></details>")
            # Extract filename
            match = re.search(r'b/(.+)$', raw_line)
            current_file = match.group(1) if match else raw_line
            html_parts.append(
                f'<details class="diff-file" open>'
                f'<summary class="diff-file-header">'
                f'<span class="diff-file-icon">📄</span> {escape(current_file)}'
                f'<span class="diff-label-pill pill-{diff_label}">{diff_label}</span>'
                f'</summary>'
                f'<div class="diff-content">'
            )
            in_hunk = False

        elif raw_line.startswith("@@"):
            flush_change_buffer()
            in_hunk = True
            html_parts.append(
                f'<div class="diff-line diff-hunk">{escape(raw_line)}</div>'
            )

        elif in_hunk:
            if raw_line.startswith("\\"):
                # Skip "\ No newline at end of file" and similar markers
                continue
            elif raw_line.startswith("-"):
                hunk_removed.append(raw_line[1:])
            elif raw_line.startswith("+"):
                hunk_added.append(raw_line[1:])
            else:
                # Context line — flush any pending changes first
                flush_change_buffer()
                html_parts.append(
                    f'<div class="diff-line diff-ctx"> {escape(raw_line[1:] if raw_line.startswith(" ") else raw_line)}</div>'
                )

    flush_change_buffer()
    if current_file:
        html_parts.append("</div></details>")

    return "\n".join(html_parts)


# ─── HTML Generation ────────────────────────────────────────────────

def parse_diff_stat(stat_output):
    """Parse the summary line of git diff --stat. Returns (files, insertions, deletions)."""
    # e.g. " 3 files changed, 10 insertions(+), 2 deletions(-)"
    files = insertions = deletions = 0
    if not stat_output:
        return files, insertions, deletions
    summary = stat_output.strip().splitlines()[-1]
    m = re.search(r'(\d+) file', summary)
    if m:
        files = int(m.group(1))
    m = re.search(r'(\d+) insertion', summary)
    if m:
        insertions = int(m.group(1))
    m = re.search(r'(\d+) deletion', summary)
    if m:
        deletions = int(m.group(1))
    return files, insertions, deletions


DASHBOARD_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  :root {
    --bg: #0a0e17;
    --surface: #111827;
    --surface2: #1a2332;
    --border: #1e2d3d;
    --text: #c9d1d9;
    --text-dim: #6b7b8d;
    --text-bright: #e6edf3;
    --accent: #58a6ff;
    --green: #3fb950;
    --green-bg: rgba(63,185,80,0.1);
    --red: #f85149;
    --red-bg: rgba(248,81,73,0.1);
    --yellow: #d29922;
    --yellow-bg: rgba(210,153,34,0.1);
    --purple: #bc8cff;
    --orange: #f0883e;
    --cyan: #39d2c0;
    /* Word-level highlight colors — high contrast against line bg */
    --wadd-bg: rgba(63,185,80,0.35);
    --wadd-border: rgba(63,185,80,0.6);
    --wdel-bg: rgba(248,81,73,0.35);
    --wdel-border: rgba(248,81,73,0.6);
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    line-height: 1.6;
    min-height: 100vh;
  }

  .dashboard {
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px;
  }

  /* ── Header ── */
  .header {
    margin-bottom: 24px;
  }
  .header-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header-left { display: flex; align-items: center; gap: 16px; }
  .header-info {
    margin-top: 14px;
    font-size: 13px;
    color: var(--text-dim);
  }
  .header-info strong { color: var(--text); }
  .header-icon {
    width: 44px; height: 44px;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px;
  }
  .repo-name, a.repo-name {
    font-size: 24px;
    font-weight: 700;
    color: var(--text-bright);
    font-family: 'JetBrains Mono', monospace;
  }
  .branch-pill {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--surface2);
    border: 1px solid var(--border);
    padding: 4px 12px;
    border-radius: 20px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    color: var(--accent);
  }
  .branch-pill::before { content: '⎇'; opacity: 0.6; }
  .header-right {
    text-align: right;
    font-size: 13px;
    color: var(--text-dim);
  }
  /* ── Stats Row ── */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 20px;
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
  }
  .stat-card.sync::before { background: var(--accent); }
  .stat-card.staged::before { background: var(--green); }
  .stat-card.modified::before { background: var(--yellow); }
  .stat-card.untracked::before { background: var(--purple); }
  .stat-card.stash::before { background: var(--orange); }
  .stat-card.commits::before { background: var(--cyan); }
  .stat-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text);
  }
  .stat-value {
    font-size: 16px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-bright);
  }
  /* Non-zero numerals take the card's key color; zero stays a neutral white. */
  .stat-card.staged .stat-value { color: var(--green); }
  .stat-card.modified .stat-value { color: var(--yellow); }
  .stat-card.untracked .stat-value { color: var(--purple); }
  .stat-card.stash .stat-value { color: var(--orange); }
  .stat-card .stat-value.zero { color: var(--text-bright); font-weight: 400; }

  /* ── Panels ── */
  .panels {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }
  @media (max-width: 900px) { .panels { grid-template-columns: 1fr; } }
  .right-stack {
    display: flex;
    flex-direction: column;
    gap: 20px;
    min-width: 0;
  }
  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .panel-full { grid-column: 1 / -1; }
  .panel-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 14px;
    color: var(--text-bright);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .panel-body {
    padding: 16px 20px;
    max-height: 500px;
    overflow-y: auto;
  }
  .diff-scroll {
    max-height: 800px;
  }
  .panel-body::-webkit-scrollbar { width: 6px; }
  .panel-body::-webkit-scrollbar-track { background: transparent; }
  .panel-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .panel-empty {
    color: var(--text-dim);
    font-style: italic;
    padding: 20px;
    text-align: center;
    font-size: 14px;
  }

  /* ── File Entries ── */
  .file-entry {
    padding: 8px 12px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .file-entry:hover { background: var(--surface2); }
  .file-entry[data-open] { cursor: pointer; }
  .file-badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .badge-staged { background: var(--green-bg); color: var(--green); }
  .badge-modified { background: var(--yellow-bg); color: var(--yellow); }
  .badge-deleted { background: var(--red-bg); color: var(--red); }
  .badge-untracked { background: rgba(188,140,255,0.1); color: var(--purple); }

  /* ── Commit Log ── */
  .commit-entry {
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 2px;
  }
  .commit-entry:hover { background: var(--surface2); }
  .commit-hash {
    font-family: 'JetBrains Mono', monospace;
    color: var(--accent);
    font-weight: 600;
    flex-shrink: 0;
  }
  a.commit-hash.commit-link {
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-color: rgba(88,166,255,0.4);
    transition: text-decoration-color 0.15s;
  }
  a.commit-hash.commit-link:hover {
    text-decoration-color: var(--accent);
  }
  .open-link {
    cursor: pointer;
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-color: rgba(88,166,255,0.4);
    transition: text-decoration-color 0.15s;
  }
  .open-link:hover { text-decoration-color: var(--accent); }
  .term-link {
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-dim);
    padding: 1px 5px;
    border-radius: 4px;
    transition: color 0.15s, background 0.15s;
  }
  .term-link:hover { color: var(--accent); background: var(--surface2); }
  .refresh-btn {
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 18px;
    line-height: 1;
    color: var(--text-dim);
    padding: 4px 8px;
    border-radius: 6px;
    user-select: none;
    transition: color 0.15s, background 0.15s;
  }
  .refresh-btn:hover { color: var(--accent); background: var(--surface2); }
  .refresh-btn.spinning { animation: refresh-spin 0.6s linear infinite; }
  @keyframes refresh-spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }
  .commit-msg {
    color: var(--text);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .commit-date {
    color: var(--text-dim);
    font-size: 12px;
    flex-shrink: 0;
  }

  /* ── Unpushed commit highlighting ── */
  .commit-unpushed .commit-hash {
    color: var(--yellow);
  }
  .commit-unpushed .commit-msg {
    color: var(--yellow);
    opacity: 0.85;
  }
  .unpushed-pill {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    flex-shrink: 0;
    background: var(--yellow-bg);
    color: var(--yellow);
  }

  /* ── Behind-main commit highlighting (bottom card) ── */
  .commit-behind-main {
    background: var(--red-bg);
    box-shadow: inset 3px 0 0 var(--red);
  }
  .commit-behind-main .commit-hash {
    color: var(--red);
  }

  /* ── Sync badges in commits panel header ── */
  .sync-badge {
    font-size: 11px;
    padding: 2px 10px;
    border-radius: 12px;
    font-weight: 600;
    margin-left: 4px;
  }
  .sync-ahead {
    background: var(--yellow-bg);
    color: var(--yellow);
  }
  .sync-behind {
    background: var(--red);
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    padding: 3px 11px;
  }
  .sync-ok {
    background: var(--green-bg);
    color: var(--green);
  }
  .sync-notrack {
    background: rgba(188,140,255,0.1);
    color: var(--purple);
  }

  /* ── Linked repo name ── */
  a.repo-name {
    text-decoration: none;
    transition: color 0.15s;
  }
  a.repo-name:hover {
    color: var(--accent);
  }

  /* ── Diff Viewer ── */
  .diff-section-label {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    padding: 12px 0 8px;
    margin-top: 8px;
  }
  .diff-section-label:first-child { margin-top: 0; }
  .diff-divider {
    height: 1px;
    background: var(--border);
    margin: 20px 0;
  }
  .diff-file { margin-bottom: 12px; }
  .diff-file-header {
    padding: 10px 14px;
    background: var(--surface2);
    border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    cursor: pointer;
    color: var(--text-bright);
    user-select: none;
    display: flex;
    align-items: center;
    gap: 8px;
    position: relative;
    overflow: hidden;
  }
  .diff-file-header::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: var(--accent);
  }
  .diff-file-header:hover { background: #1f2e40; }
  .diff-file[open] > .diff-file-header { border-radius: 6px 6px 0 0; }
  .diff-file-icon { flex-shrink: 0; }
  .diff-label-pill {
    margin-left: auto;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
  }
  .pill-staged { background: var(--green-bg); color: var(--green); }
  .pill-unstaged { background: var(--yellow-bg); color: var(--yellow); }
  .pill-range { background: rgba(88,166,255,0.1); color: var(--accent); }

  .diff-content {
    border: 1px solid var(--border);
    border-top: none;
    border-radius: 0 0 6px 6px;
    overflow-x: auto;
  }
  .diff-line {
    padding: 1px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    border-left: 3px solid transparent;
  }
  .diff-add {
    background: var(--green-bg);
    color: var(--green);
    border-left-color: var(--green);
  }
  .diff-del {
    background: var(--red-bg);
    color: var(--red);
    border-left-color: var(--red);
  }
  .diff-hunk {
    background: rgba(88,166,255,0.08);
    color: var(--accent);
    padding: 6px 14px;
    font-weight: 600;
    border-left-color: var(--accent);
  }
  .diff-ctx { color: var(--text-dim); }

  /* ── WORD-LEVEL HIGHLIGHTS — the key feature ── */
  mark.wadd {
    background: var(--wadd-bg);
    color: #7ee787;
    border-radius: 3px;
    padding: 1px 3px;
    margin: 0 1px;
    border-bottom: 2px solid var(--wadd-border);
    text-decoration: none;
  }
  mark.wdel {
    background: var(--wdel-bg);
    color: #ffa198;
    border-radius: 3px;
    padding: 1px 3px;
    margin: 0 1px;
    border-bottom: 2px solid var(--wdel-border);
    text-decoration: none;
  }

"""


PAGE_TEMPLATE = string.Template('''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Git Dashboard — $title</title>
<style>$css</style>
</head>
<body>
<div class="dashboard">

  <div class="header">
    <div class="header-top">
      <div class="header-left">
        <div class="header-icon">⌥</div>
        $repo_name
        $refresh
        <span class="branch-pill">$branch_label</span>
      </div>
      <div class="header-right">
        Generated $now
      </div>
    </div>
    <div class="header-info">
      <strong>Remote:</strong> $remote
      &nbsp;&nbsp;·&nbsp;&nbsp;
      <strong>Path:</strong> $path $terminal
    </div>
  </div>

  <div class="stats-row">
    $stats_row
  </div>

  <div class="panels">
    <div class="panel">
      <div class="panel-header">📋 Working Tree Status</div>
      <div class="panel-body">
        $file_status
      </div>
    </div>
    $right_column
  </div>

  $diff_section

</div>
</body>
</html>''')


def generate_html(info, repo_path, range_spec=None, live=False):
    """Generate the complete dashboard HTML."""

    # Get raw diffs — range mode vs working tree mode
    is_range = range_spec is not None
    range_diff_html = ""
    range_files = range_insertions = range_deletions = 0
    staged_diff_html = ""
    unstaged_diff_html = ""

    if is_range:
        # Determine if this is a two-dot committed range or open-ended (includes working tree)
        if ".." in range_spec:
            # Two-dot range: committed changes only
            range_diff = run_git(["diff", range_spec], repo_path)
            range_stat = run_git(["diff", "--stat", range_spec], repo_path)
            range_label = range_spec
        else:
            # Single ref: diff from that ref to working tree
            range_diff = run_git(["diff", range_spec], repo_path)
            range_stat = run_git(["diff", "--stat", range_spec], repo_path)
            range_label = f"{range_spec}..working tree"
        range_diff_html = parse_and_render_diff(range_diff, "range")
        range_files, range_insertions, range_deletions = parse_diff_stat(range_stat)
    else:
        staged_diff = run_git(["diff", "--cached"], repo_path)
        unstaged_diff = run_git(["diff"], repo_path)
        staged_diff_html = parse_and_render_diff(staged_diff, "staged")
        unstaged_diff_html = parse_and_render_diff(unstaged_diff, "unstaged")

    def zero_class(n):
        return " zero" if n == 0 else ""

    staged_count = len(info["staged_files"])
    modified_count = len(info["modified_files"])
    untracked_count = len(info["untracked_files"])

    # Build file status HTML
    def status_badge(line, default_label, default_class):
        # name-status lines start with a status code (M, A, D, R100, ...) + tab
        code = line[:1]
        if code == "D":
            return "deleted", "badge-deleted"
        return default_label, default_class

    def open_attr(rel_path):
        # data-open makes a file row click-to-reveal — only in live mode and
        # only when the file is actually on disk (so deletions get no link).
        if live and os.path.exists(os.path.join(repo_path, rel_path)):
            return f' data-open="{escape(rel_path)}"'
        return ""

    file_status_html = ""
    for line in (info["staged_status"].splitlines() if info["staged_status"] else []):
        label, cls = status_badge(line, "staged", "badge-staged")
        attr = open_attr(line.split("\t")[-1])
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge {cls}">{label}</span> {escape(line)}</div>\n'
    for line in (info["modified_status"].splitlines() if info["modified_status"] else []):
        label, cls = status_badge(line, "modified", "badge-modified")
        attr = open_attr(line.split("\t")[-1])
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge {cls}">{label}</span> {escape(line)}</div>\n'
    for f in info["untracked_files"]:
        attr = open_attr(f)
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge badge-untracked">untracked</span> {escape(f)}</div>\n'

    if not file_status_html:
        file_status_html = '<div class="panel-empty">✓ Working tree clean</div>'

    # Commits panel header with sync status
    ahead, behind = info["ahead"], info["behind"]
    sync_parts = []
    if ahead > 0:
        sync_parts.append(f'<span class="sync-badge sync-ahead">↑ {ahead} unpushed</span>')
    if behind > 0:
        sync_parts.append(f'<span class="sync-badge sync-behind">↓ {behind} behind</span>')
    if not sync_parts and info["tracking"]:
        sync_parts.append('<span class="sync-badge sync-ok">In sync</span>')
    elif not info["tracking"]:
        sync_parts.append('<span class="sync-badge sync-notrack">No remote</span>')
    commits_header_extra = " ".join(sync_parts)

    # Commits HTML — unpushed highlighted, pushed linked to GitHub
    github_url = info["github_url"]
    commits_html = ""
    for c in info["recent_commits"]:
        is_unpushed = c["full_hash"] in info["unpushed_hashes"]
        entry_class = "commit-entry commit-unpushed" if is_unpushed else "commit-entry"

        if is_unpushed or not github_url:
            hash_html = f'<span class="commit-hash">{escape(c["hash"])}</span>'
        else:
            commit_url = f'{github_url}/commit/{c["full_hash"]}'
            hash_html = f'<a class="commit-hash commit-link" href="{escape(commit_url)}" target="_blank">{escape(c["hash"])}</a>'

        commits_html += (
            f'<div class="{entry_class}">'
            f'{hash_html}'
            f'<span class="commit-msg">{escape(c["msg"])}</span>'
            f'<span class="commit-date">{escape(c["date"])}</span>'
            f'</div>\n'
        )

    # Right column — either a single "Recent Commits" panel (on the default
    # branch, or no default branch detected) or two stacked panels: top
    # "⎇ Branch" for the current branch, bottom "📜 <main>" comparing HEAD
    # against the default ref.
    show_main = info["show_main_card"]
    branch_title = "⎇ Branch" if show_main else "📜 Recent Commits"
    if show_main and not commits_html:
        branch_body = '<div class="panel-empty">No commits on this branch yet</div>'
    else:
        branch_body = commits_html
    branch_panel_html = (
        f'<div class="panel">'
        f'<div class="panel-header">{branch_title} {commits_header_extra}</div>'
        f'<div class="panel-body">{branch_body}</div>'
        f'</div>'
    )

    main_panel_html = ""
    if show_main:
        default_name = info["default_branch"]
        behind = info["behind_main"]
        if behind > 0:
            main_badge = (
                f'<span class="sync-badge sync-behind">'
                f'↓ {behind} behind {escape(default_name)}</span>'
            )
        else:
            main_badge = (
                f'<span class="sync-badge sync-ok">'
                f'In sync with {escape(default_name)}</span>'
            )

        main_commits_html = ""
        behind_hashes = info["behind_main_hashes"]
        for c in info["main_recent_commits"]:
            is_behind = c["full_hash"] in behind_hashes
            entry_class = (
                "commit-entry commit-behind-main" if is_behind else "commit-entry"
            )
            if github_url:
                commit_url = f'{github_url}/commit/{c["full_hash"]}'
                hash_html = (
                    f'<a class="commit-hash commit-link" '
                    f'href="{escape(commit_url)}" target="_blank">'
                    f'{escape(c["hash"])}</a>'
                )
            else:
                hash_html = f'<span class="commit-hash">{escape(c["hash"])}</span>'
            main_commits_html += (
                f'<div class="{entry_class}">'
                f'{hash_html}'
                f'<span class="commit-msg">{escape(c["msg"])}</span>'
                f'<span class="commit-date">{escape(c["date"])}</span>'
                f'</div>\n'
            )
        if not main_commits_html:
            main_commits_html = '<div class="panel-empty">No commits</div>'

        main_panel_html = (
            f'<div class="panel">'
            f'<div class="panel-header">📜 {escape(default_name)} {main_badge}</div>'
            f'<div class="panel-body">{main_commits_html}</div>'
            f'</div>'
        )

    right_column_html = (
        f'<div class="right-stack">{branch_panel_html}{main_panel_html}</div>'
    )

    # Diff section — range mode or working tree mode
    diff_section = ""
    if is_range:
        if range_diff_html:
            diff_section = f'<div class="panel panel-full"><div class="panel-header">🔍 Diff Viewer — {escape(range_label)}</div>\n'
            diff_section += '<div class="panel-body diff-scroll">\n'
            diff_section += f'<div class="diff-section-label">{range_files} file{"s" if range_files != 1 else ""} changed, +{range_insertions} −{range_deletions}</div>\n'
            diff_section += range_diff_html + "\n"
            diff_section += '</div></div>\n'
        else:
            diff_section = (
                '<div class="panel panel-full">'
                f'<div class="panel-header">🔍 Diff Viewer — {escape(range_label)}</div>'
                '<div class="panel-body"><div class="panel-empty">No changes in this range</div></div>'
                '</div>'
            )
    elif staged_diff_html or unstaged_diff_html:
        diff_section = '<div class="panel panel-full"><div class="panel-header">🔍 Diff Viewer — Word-Level Change Detection</div>\n'
        diff_section += '<div class="panel-body diff-scroll">\n'

        if staged_diff_html:
            diff_section += f'<div class="diff-section-label">Staged Changes ({staged_count} file{"s" if staged_count != 1 else ""})</div>\n'
            diff_section += staged_diff_html + "\n"

        if staged_diff_html and unstaged_diff_html:
            diff_section += '<div class="diff-divider"></div>\n'

        if unstaged_diff_html:
            diff_section += f'<div class="diff-section-label">Unstaged Changes ({modified_count} file{"s" if modified_count != 1 else ""})</div>\n'
            diff_section += unstaged_diff_html + "\n"

        diff_section += '</div></div>\n'
    else:
        diff_section = (
            '<div class="panel panel-full">'
            '<div class="panel-header">🔍 Diff Viewer</div>'
            '<div class="panel-body"><div class="panel-empty">No uncommitted changes to diff</div></div>'
            '</div>'
        )

    # Stats row — compact horizontal cards; different set for range vs working tree
    if is_range:
        stats_row_html = f'''
    <div class="stat-card modified">
      <div class="stat-label">Files Changed</div>
      <div class="stat-value{zero_class(range_files)}">{range_files}</div>
    </div>
    <div class="stat-card staged">
      <div class="stat-label">Insertions</div>
      <div class="stat-value{zero_class(range_insertions)}">+{range_insertions}</div>
    </div>
    <div class="stat-card untracked">
      <div class="stat-label">Deletions</div>
      <div class="stat-value{zero_class(range_deletions)}">−{range_deletions}</div>
    </div>
    <div class="stat-card stash">
      <div class="stat-label">Stashes</div>
      <div class="stat-value{zero_class(info["stash_count"])}">{info["stash_count"]}</div>
    </div>'''
    else:
        stats_row_html = f'''
    <div class="stat-card staged">
      <div class="stat-label">Staged</div>
      <div class="stat-value{zero_class(staged_count)}">{staged_count}</div>
    </div>
    <div class="stat-card modified">
      <div class="stat-label">Modified</div>
      <div class="stat-value{zero_class(modified_count)}">{modified_count}</div>
    </div>
    <div class="stat-card untracked">
      <div class="stat-label">Untracked</div>
      <div class="stat-value{zero_class(untracked_count)}">{untracked_count}</div>
    </div>
    <div class="stat-card stash">
      <div class="stat-label">Stashes</div>
      <div class="stat-value{zero_class(info["stash_count"])}">{info["stash_count"]}</div>
    </div>'''

    now = datetime.now().strftime("%B %d, %Y at %I:%M:%S %p")

    # Repo name — linked to GitHub if available
    if info["github_url"]:
        repo_name_html = f'<a class="repo-name" href="{escape(info["github_url"])}" target="_blank">{escape(info["repo_name"])}</a>'
    else:
        repo_name_html = f'<div class="repo-name">{escape(info["repo_name"])}</div>'

    # Branch pill — just the branch name, unless it tracks a non-obvious
    # upstream (a fork's upstream, a differently-named remote branch, …), in
    # which case show "branch → upstream" so the mismatch is visible.
    branch = info["branch"]
    tracking = info["tracking"]
    if tracking and tracking != f"origin/{branch}":
        branch_label = f"{branch} → {tracking}"
    else:
        branch_label = branch

    # Path — click-to-reveal link in live (watch) mode, plain text otherwise;
    # followed by a terminal launcher icon.
    path_str = escape(str(repo_path))
    if live:
        path_html = f'<span class="open-link" data-open=".">{path_str}</span>'
        terminal_html = (
            f'<span class="term-link" data-open="." data-app="terminal"'
            f' title="Open in {escape(TERMINAL_APP)}">🖥️</span>'
        )
        refresh_html = (
            '<span class="refresh-btn" data-refresh '
            'title="Re-scan repository">↻</span>'
        )
    else:
        path_html = path_str
        terminal_html = ""
        refresh_html = ""

    html = PAGE_TEMPLATE.substitute(
        css=DASHBOARD_CSS,
        title=escape(info["repo_name"]),
        repo_name=repo_name_html,
        branch_label=escape(branch_label),
        refresh=refresh_html,
        now=now,
        remote=escape(info["remote_url"]),
        path=path_html,
        terminal=terminal_html,
        stats_row=stats_row_html,
        file_status=file_status_html,
        right_column=right_column_html,
        diff_section=diff_section,
    )

    return html


# ─── Watch Mode Server ──────────────────────────────────────────────

def compute_state_hash(repo_path):
    """
    Cheap fingerprint of "everything the dashboard cares about."

    Porcelain status only flags whether a file is modified, not what changed
    inside it — so repeated edits to an already-modified file would otherwise
    be invisible. Folding in full diff output (working tree + index) catches
    content edits. `diff --raw` looks cheaper but uses 0000000 for unstaged
    post-image SHAs, so it can't distinguish successive edits.

    The upstream ref is included so a push (which moves the remote-tracking
    branch but touches neither HEAD nor the working tree) triggers a refresh —
    otherwise the dashboard keeps showing the just-pushed commits as unpushed.

    The default branch ref (origin/main or local main) is included so the
    bottom "main" card refreshes when collaborators push to main, or when
    main moves in a parallel terminal session in a no-remote repo.

    --untracked-files=all is required: the default ("normal") collapses
    untracked directories to a single entry, so adding a file inside an
    already-untracked directory wouldn't change the hash and the poll
    loop would never trigger a refresh. (Cheap in practice — .gitignore
    is still honored, so node_modules etc. are not enumerated.)
    """
    status = run_git(
        ["status", "--porcelain", "--untracked-files=all"], repo_path
    )
    head = run_git(["rev-parse", "HEAD"], repo_path)
    upstream = run_git(["rev-parse", "@{upstream}"], repo_path)
    _, default_ref = find_default_ref(repo_path)
    default_head = run_git(["rev-parse", default_ref], repo_path) if default_ref else ""
    unstaged = run_git(["diff"], repo_path)
    staged = run_git(["diff", "--cached"], repo_path)
    return hashlib.sha1(
        f"{status}|{head}|{upstream}|{default_head}|{unstaged}|{staged}".encode()
    ).hexdigest()


def inject_watch_script(html):
    """Splice the SSE auto-reload client into the dashboard HTML."""
    script = """<script>
(function() {
  // Restore scroll position from before the reload
  var saved = sessionStorage.getItem('git-dashboard-scroll');
  if (saved !== null) {
    window.scrollTo(0, parseInt(saved, 10));
    sessionStorage.removeItem('git-dashboard-scroll');
  }
  var es = new EventSource('/events');
  es.onmessage = function(e) {
    if (e.data === 'refresh') {
      sessionStorage.setItem('git-dashboard-scroll', window.scrollY);
      location.reload();
    }
  };
  // Click anything tagged data-open to act on it via /open — reveal in
  // Finder, or open a terminal there when data-app is set.
  document.addEventListener('click', function(e) {
    var refresh = e.target.closest('[data-refresh]');
    if (refresh) {
      refresh.classList.add('spinning');
      fetch('/refresh');
      return;
    }
    var el = e.target.closest('[data-open]');
    if (!el) return;
    var url = '/open?path=' + encodeURIComponent(el.getAttribute('data-open'));
    var app = el.getAttribute('data-app');
    if (app) url += '&app=' + encodeURIComponent(app);
    fetch(url);
  });
})();
</script>
</body>"""
    return html.replace("</body>", script, 1)


class WatchState:
    """Shared state between the HTTP server, SSE clients, and polling thread."""

    def __init__(self, repo_path, range_spec):
        self.repo_path = repo_path
        self.range_spec = range_spec
        self.html = b""
        self.html_lock = threading.Lock()
        self.clients = []           # list of queue.Queue, one per SSE client
        self.clients_lock = threading.Lock()
        self.last_hash = None
        self.shutdown_event = threading.Event()
        self.allowed_paths = set()  # realpath'd paths /open may act on
        self.allowed_lock = threading.Lock()

    def regenerate(self):
        """Rebuild the dashboard HTML from current repo state."""
        info = collect_repo_info(self.repo_path)
        self._rebuild_allowed_paths(info)
        html = generate_html(info, self.repo_path,
                             range_spec=self.range_spec, live=True)
        html = inject_watch_script(html)
        with self.html_lock:
            self.html = html.encode("utf-8")

    def force_refresh(self):
        """Re-scan and tell every client to reload, regardless of whether the
        watched state actually changed. Triggered by the header ↻ button when
        the user suspects the dashboard is stale — e.g. a local commit on a
        non-tracked default branch that the poll hash doesn't notice.

        last_hash is recomputed *after* regenerate(), because regenerate runs
        `git fetch` — if that pulls new commits, the upstream ref moves and a
        pre-fetch hash would mismatch on the very next poll tick, causing a
        spurious second refresh."""
        self.regenerate()
        self.last_hash = compute_state_hash(self.repo_path)
        self.broadcast("refresh")
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] Refreshed (manual)", flush=True)

    def _rebuild_allowed_paths(self, info):
        """Refresh the set of paths /open may reveal: the repo root plus
        every changed file that currently exists on disk."""
        allowed = {os.path.realpath(self.repo_path)}
        for rel in (info["staged_files"] + info["modified_files"]
                    + info["untracked_files"]):
            p = os.path.join(self.repo_path, rel)
            if os.path.exists(p):
                allowed.add(os.path.realpath(p))
        with self.allowed_lock:
            self.allowed_paths = allowed

    def is_allowed(self, abspath):
        """True if abspath is something /open is permitted to reveal."""
        with self.allowed_lock:
            return abspath in self.allowed_paths

    def get_html(self):
        with self.html_lock:
            return self.html

    def add_client(self):
        q = queue.Queue()
        with self.clients_lock:
            self.clients.append(q)
        return q

    def remove_client(self, q):
        with self.clients_lock:
            if q in self.clients:
                self.clients.remove(q)

    def broadcast(self, message):
        """Send a message to every connected SSE client."""
        with self.clients_lock:
            targets = list(self.clients)
        for q in targets:
            q.put(message)


def poll_loop(state):
    """
    Background thread: every POLL_INTERVAL_SECONDS, recompute the state hash.
    If it changed, regenerate the HTML and tell every browser tab to reload.
    """
    while not state.shutdown_event.is_set():
        try:
            current_hash = compute_state_hash(state.repo_path)
            if current_hash != state.last_hash:
                state.last_hash = current_hash
                state.regenerate()
                state.broadcast("refresh")
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{timestamp}] Refreshed", flush=True)
        except Exception as e:
            sys.stderr.write(f"poll error: {e}\n")
        # Sleep, but wake immediately on shutdown
        state.shutdown_event.wait(POLL_INTERVAL_SECONDS)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """
    Like ThreadingHTTPServer, but doesn't dump a traceback every time a
    browser closes a connection — that happens routinely (speculative
    connections, SSE teardown on reload) and isn't something we can or
    should fix from the client side.
    """

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(
            exc_type, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
        ):
            return  # Normal client disconnect; not worth logging.
        super().handle_error(request, client_address)


def make_request_handler(state):
    """Build a BaseHTTPRequestHandler subclass bound to a WatchState."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Suppress per-request access logging; the refresh prints suffice.
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._serve_dashboard()
            elif self.path == "/events":
                self._serve_events()
            elif self.path.startswith("/open?"):
                self._serve_open()
            elif self.path == "/refresh":
                state.force_refresh()
                self.send_response(204)
                self.end_headers()
            else:
                self.send_error(404)

        def _serve_dashboard(self):
            body = state.get_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _serve_events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = state.add_client()
            try:
                # Initial comment confirms the SSE stream is alive
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not state.shutdown_event.is_set():
                    try:
                        msg = q.get(timeout=30)
                    except queue.Empty:
                        msg = "__keepalive__"
                    if msg == "__shutdown__":
                        break
                    if msg == "__keepalive__":
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Browser tab closed; nothing to clean up beyond removal.
            finally:
                state.remove_client(q)

        def _serve_open(self):
            """Act on an allowlisted path: reveal in Finder (open -R for a
            file, open for the repo folder), or open a terminal there
            (app=terminal). Guarded by state.is_allowed."""
            params = parse_qs(urlparse(self.path).query)
            rel = (params.get("path") or [""])[0]
            app = (params.get("app") or [""])[0]
            if not rel:
                self.send_error(400)
                return
            abspath = os.path.realpath(os.path.join(state.repo_path, rel))
            if not state.is_allowed(abspath):
                self.send_error(403)
                return
            if app == "terminal":
                cmd = ["open", "-a", TERMINAL_APP, abspath]
            elif os.path.isdir(abspath):
                cmd = ["open", abspath]
            else:
                cmd = ["open", "-R", abspath]
            try:
                subprocess.run(cmd, timeout=5)
            except Exception:
                self.send_error(500)
                return
            self.send_response(204)
            self.end_headers()

    return Handler


def serve_watch(repo_path, range_spec):
    """Run the watch server until the user hits Ctrl-C."""
    state = WatchState(repo_path, range_spec)

    print(f"Scanning {repo_path}...", flush=True)
    state.last_hash = compute_state_hash(repo_path)
    state.regenerate()

    handler = make_request_handler(state)
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    poll_thread = threading.Thread(target=poll_loop, args=(state,), daemon=True)
    poll_thread.start()

    print(f"Dashboard: {url}", flush=True)
    print(f"Watching for changes (polling every {POLL_INTERVAL_SECONDS}s) — Ctrl-C to stop.", flush=True)
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        state.shutdown_event.set()
        # Wake any SSE client threads blocked on their queue
        with state.clients_lock:
            for q in state.clients:
                q.put("__shutdown__")
        server.shutdown()
        server.server_close()


# ─── Main ───────────────────────────────────────────────────────────

HELP_TEXT = __doc__.strip()


def parse_args(argv):
    """Parse command-line arguments. Returns (range_spec, path, once).

    once=False means watch mode (the default); once=True means one-shot.
    --range implies once=True since history can't be meaningfully watched.
    """
    args = argv[1:]  # skip script name

    if not args:
        return None, ".", False  # no flags → watch mode

    if args[0] in ("--help", "-h"):
        print(HELP_TEXT)
        sys.exit(0)

    # --once can appear anywhere; pull it out before positional parsing.
    once = "--once" in args
    if once:
        args = [a for a in args if a != "--once"]

    range_spec = None
    path = "."

    if args and args[0] == "--range":
        if len(args) < 2:
            print("Error: --range requires an argument (e.g. HEAD~3..HEAD)")
            sys.exit(1)
        range_spec = args[1]
        # Normalize open-ended: "HEAD~3.." → "HEAD~3"
        if range_spec.endswith(".."):
            range_spec = range_spec[:-2]
        if len(args) > 2:
            path = args[2]
    elif args:
        path = args[0]

    # --range implies --once (no point watching a fixed historical range).
    if range_spec:
        once = True

    return range_spec, os.path.abspath(path), once


def main():
    range_spec, start_path, once = parse_args(sys.argv)
    start_path = os.path.abspath(start_path)

    # Resolve to repo root from wherever we are inside the checkout
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start_path, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Error: {start_path} is not inside a Git repository.")
        sys.exit(1)

    repo_path = result.stdout.strip()

    if not once:
        serve_watch(repo_path, range_spec)
        return

    if range_spec:
        print(f"Scanning {repo_path} (range: {range_spec})...")
    else:
        print(f"Scanning {repo_path}...")
    info = collect_repo_info(repo_path)
    html = generate_html(info, repo_path, range_spec=range_spec)

    safe_name = re.sub(r'[^\w\-]', '-', info["repo_name"]).strip('-')
    output_path = f"/tmp/{safe_name}-git-dashboard.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard: {output_path}")
    webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    main()
