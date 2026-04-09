#!/usr/bin/env python3
"""
git-dashboard.py — Git status dashboard with word-level diff highlighting.

Generates an HTML dashboard showing repository status, file changes, and
critically: sub-line diffs that highlight exactly which words changed within
modified paragraphs. No more squinting at entire red/green paragraphs to
find the one curly quote that got straightened.

Usage:
    python3 git-dashboard.py [path-to-repo]
    python3 git-dashboard.py --range HEAD~3..HEAD [path-to-repo]
    python3 git-dashboard.py --range HEAD~3 [path-to-repo]

Options:
    --range <spec>   Show diffs for a commit range instead of working tree.
                     Two-dot range (HEAD~3..HEAD) shows committed changes only.
                     Single ref (HEAD~3) or open-ended (HEAD~3..) includes
                     uncommitted working tree changes as well.
    --help, -h       Show this help message and exit.

Opens in default browser automatically on macOS.
"""

import subprocess
import sys
import os
import re
import webbrowser
from datetime import datetime
from difflib import SequenceMatcher
from html import escape
from pathlib import Path


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

    # Recent commits (include full hash for GitHub links)
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
    info["github_url"] = ""
    remote = info["remote_url"]
    if "github.com" in remote:
        # Handle SSH: git@github.com:user/repo.git
        # Handle HTTPS: https://github.com/user/repo.git
        gh = remote
        gh = re.sub(r'^git@github\.com:', 'https://github.com/', gh)
        gh = re.sub(r'\.git$', '', gh)
        info["github_url"] = gh

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


def generate_html(info, repo_path, range_spec=None):
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
    file_status_html = ""
    for line in (info["staged_status"].splitlines() if info["staged_status"] else []):
        file_status_html += f'<div class="file-entry"><span class="file-badge badge-staged">staged</span> {escape(line)}</div>\n'
    for line in (info["modified_status"].splitlines() if info["modified_status"] else []):
        file_status_html += f'<div class="file-entry"><span class="file-badge badge-modified">modified</span> {escape(line)}</div>\n'
    for f in info["untracked_files"]:
        file_status_html += f'<div class="file-entry"><span class="file-badge badge-untracked">untracked</span> {escape(f)}</div>\n'

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

    # Diff section — range mode or working tree mode
    diff_legend = (
        '<div class="diff-legend">'
        '<span class="legend-item"><mark class="wadd">added text</mark> Changed words (new)</span>'
        '<span class="legend-item"><mark class="wdel">removed text</mark> Changed words (old)</span>'
        '<span class="legend-item"><span class="legend-swatch swatch-add"></span> Added line</span>'
        '<span class="legend-item"><span class="legend-swatch swatch-del"></span> Removed line</span>'
        '</div>\n'
    )

    diff_section = ""
    if is_range:
        if range_diff_html:
            diff_section = f'<div class="panel panel-full"><div class="panel-header">🔍 Diff Viewer — {escape(range_label)}</div>\n'
            diff_section += diff_legend
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
        diff_section += diff_legend
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

    # Stats row — different cards for range vs working tree mode
    if is_range:
        stats_row_html = f'''
    <div class="stat-card modified">
      <div class="stat-label">Files Changed</div>
      <div class="stat-value{zero_class(range_files)}">{range_files}</div>
      <div class="stat-detail">In {escape(range_label)}</div>
    </div>
    <div class="stat-card staged">
      <div class="stat-label">Insertions</div>
      <div class="stat-value{zero_class(range_insertions)}">+{range_insertions}</div>
      <div class="stat-detail">Lines added</div>
    </div>
    <div class="stat-card untracked">
      <div class="stat-label">Deletions</div>
      <div class="stat-value{zero_class(range_deletions)}">−{range_deletions}</div>
      <div class="stat-detail">Lines removed</div>
    </div>
    <div class="stat-card stash">
      <div class="stat-label">Stashes</div>
      <div class="stat-value{zero_class(info["stash_count"])}">{info["stash_count"]}</div>
      <div class="stat-detail">Saved for later</div>
    </div>'''
    else:
        stats_row_html = f'''
    <div class="stat-card staged">
      <div class="stat-label">Staged</div>
      <div class="stat-value{zero_class(staged_count)}">{staged_count}</div>
      <div class="stat-detail">Ready to commit</div>
    </div>
    <div class="stat-card modified">
      <div class="stat-label">Modified</div>
      <div class="stat-value{zero_class(modified_count)}">{modified_count}</div>
      <div class="stat-detail">Unstaged changes</div>
    </div>
    <div class="stat-card untracked">
      <div class="stat-label">Untracked</div>
      <div class="stat-value{zero_class(untracked_count)}">{untracked_count}</div>
      <div class="stat-detail">New files</div>
    </div>
    <div class="stat-card stash">
      <div class="stat-label">Stashes</div>
      <div class="stat-value{zero_class(info["stash_count"])}">{info["stash_count"]}</div>
      <div class="stat-detail">Saved for later</div>
    </div>'''

    now = datetime.now().strftime("%B %d, %Y at %I:%M:%S %p")

    # Repo name — linked to GitHub if available
    if info["github_url"]:
        repo_name_html = f'<a class="repo-name" href="{escape(info["github_url"])}" target="_blank">{escape(info["repo_name"])}</a>'
    else:
        repo_name_html = f'<div class="repo-name">{escape(info["repo_name"])}</div>'

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Git Dashboard — {escape(info["repo_name"])}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&display=swap');

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  :root {{
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
  }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    line-height: 1.6;
    min-height: 100vh;
  }}

  .dashboard {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px;
  }}

  /* ── Header ── */
  .header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 0 24px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }}
  .header-left {{ display: flex; align-items: center; gap: 16px; }}
  .header-icon {{
    width: 44px; height: 44px;
    background: linear-gradient(135deg, var(--accent), var(--purple));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px;
  }}
  .repo-name, a.repo-name {{
    font-size: 24px;
    font-weight: 700;
    color: var(--text-bright);
    font-family: 'JetBrains Mono', monospace;
    display: block;
  }}
  .branch-pill {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--surface2);
    border: 1px solid var(--border);
    padding: 4px 12px;
    border-radius: 20px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    color: var(--accent);
  }}
  .branch-pill::before {{ content: '⎇'; opacity: 0.6; }}
  .header-right {{
    text-align: right;
    font-size: 13px;
    color: var(--text-dim);
  }}
  .refresh-btn {{
    display: inline-block;
    margin-top: 6px;
    padding: 6px 14px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--accent);
    font-size: 12px;
    font-family: 'DM Sans', sans-serif;
    cursor: pointer;
    transition: background 0.15s;
  }}
  .refresh-btn:hover {{ background: var(--surface); }}

  /* ── Stats Row ── */
  .stats-row {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    position: relative;
    overflow: hidden;
  }}
  .stat-card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
  }}
  .stat-card.sync::before {{ background: var(--accent); }}
  .stat-card.staged::before {{ background: var(--green); }}
  .stat-card.modified::before {{ background: var(--yellow); }}
  .stat-card.untracked::before {{ background: var(--purple); }}
  .stat-card.stash::before {{ background: var(--orange); }}
  .stat-card.commits::before {{ background: var(--cyan); }}
  .stat-label {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim);
    margin-bottom: 6px;
  }}
  .stat-value {{
    font-size: 28px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-bright);
  }}
  .stat-detail {{
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 4px;
  }}
  .stat-value.sync-ok {{ color: var(--green); }}
  .stat-value.sync-warn {{ color: var(--yellow); }}
  .stat-value.sync-danger {{ color: var(--red); }}
  .stat-value.zero {{ color: var(--text-dim); opacity: 0.5; }}

  /* ── Panels ── */
  .panels {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }}
  @media (max-width: 900px) {{ .panels {{ grid-template-columns: 1fr; }} }}
  .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }}
  .panel-full {{ grid-column: 1 / -1; }}
  .panel-header {{
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 14px;
    color: var(--text-bright);
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .panel-body {{
    padding: 16px 20px;
    max-height: 500px;
    overflow-y: auto;
  }}
  .diff-scroll {{
    max-height: 800px;
  }}
  .panel-body::-webkit-scrollbar {{ width: 6px; }}
  .panel-body::-webkit-scrollbar-track {{ background: transparent; }}
  .panel-body::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
  .panel-empty {{
    color: var(--text-dim);
    font-style: italic;
    padding: 20px;
    text-align: center;
    font-size: 14px;
  }}

  /* ── File Entries ── */
  .file-entry {{
    padding: 8px 12px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .file-entry:hover {{ background: var(--surface2); }}
  .file-badge {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    flex-shrink: 0;
  }}
  .badge-staged {{ background: var(--green-bg); color: var(--green); }}
  .badge-modified {{ background: var(--yellow-bg); color: var(--yellow); }}
  .badge-untracked {{ background: rgba(188,140,255,0.1); color: var(--purple); }}

  /* ── Commit Log ── */
  .commit-entry {{
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 2px;
  }}
  .commit-entry:hover {{ background: var(--surface2); }}
  .commit-hash {{
    font-family: 'JetBrains Mono', monospace;
    color: var(--accent);
    font-weight: 600;
    flex-shrink: 0;
  }}
  a.commit-hash.commit-link {{
    text-decoration: underline;
    text-underline-offset: 3px;
    text-decoration-color: rgba(88,166,255,0.4);
    transition: text-decoration-color 0.15s;
  }}
  a.commit-hash.commit-link:hover {{
    text-decoration-color: var(--accent);
  }}
  .commit-msg {{
    color: var(--text);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .commit-date {{
    color: var(--text-dim);
    font-size: 12px;
    flex-shrink: 0;
  }}

  /* ── Unpushed commit highlighting ── */
  .commit-unpushed .commit-hash {{
    color: var(--yellow);
  }}
  .commit-unpushed .commit-msg {{
    color: var(--yellow);
    opacity: 0.85;
  }}
  .unpushed-pill {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    flex-shrink: 0;
    background: var(--yellow-bg);
    color: var(--yellow);
  }}

  /* ── Sync badges in commits panel header ── */
  .sync-badge {{
    font-size: 11px;
    padding: 2px 10px;
    border-radius: 12px;
    font-weight: 600;
    margin-left: 4px;
  }}
  .sync-ahead {{
    background: var(--yellow-bg);
    color: var(--yellow);
  }}
  .sync-behind {{
    background: var(--red-bg);
    color: var(--red);
  }}
  .sync-ok {{
    background: var(--green-bg);
    color: var(--green);
  }}
  .sync-notrack {{
    background: rgba(188,140,255,0.1);
    color: var(--purple);
  }}

  /* ── Linked repo name ── */
  a.repo-name {{
    text-decoration: none;
    transition: color 0.15s;
  }}
  a.repo-name:hover {{
    color: var(--accent);
  }}

  /* ── Diff Viewer ── */
  .diff-section-label {{
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    padding: 12px 0 8px;
    margin-top: 8px;
  }}
  .diff-section-label:first-child {{ margin-top: 0; }}
  .diff-divider {{
    height: 1px;
    background: var(--border);
    margin: 20px 0;
  }}
  .diff-legend {{
    display: flex;
    gap: 20px;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    color: var(--text-dim);
    flex-wrap: wrap;
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .legend-swatch {{
    display: inline-block;
    width: 14px;
    height: 14px;
    border-radius: 3px;
  }}
  .swatch-add {{ background: var(--green-bg); border-left: 3px solid var(--green); }}
  .swatch-del {{ background: var(--red-bg); border-left: 3px solid var(--red); }}

  .diff-file {{ margin-bottom: 12px; }}
  .diff-file-header {{
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
  }}
  .diff-file-header::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: var(--accent);
  }}
  .diff-file-header:hover {{ background: #1f2e40; }}
  .diff-file[open] > .diff-file-header {{ border-radius: 6px 6px 0 0; }}
  .diff-file-icon {{ flex-shrink: 0; }}
  .diff-label-pill {{
    margin-left: auto;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
  }}
  .pill-staged {{ background: var(--green-bg); color: var(--green); }}
  .pill-unstaged {{ background: var(--yellow-bg); color: var(--yellow); }}
  .pill-range {{ background: rgba(88,166,255,0.1); color: var(--accent); }}

  .diff-content {{
    border: 1px solid var(--border);
    border-top: none;
    border-radius: 0 0 6px 6px;
    overflow-x: auto;
  }}
  .diff-line {{
    padding: 1px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    border-left: 3px solid transparent;
  }}
  .diff-add {{
    background: var(--green-bg);
    color: var(--green);
    border-left-color: var(--green);
  }}
  .diff-del {{
    background: var(--red-bg);
    color: var(--red);
    border-left-color: var(--red);
  }}
  .diff-hunk {{
    background: rgba(88,166,255,0.08);
    color: var(--accent);
    padding: 6px 14px;
    font-weight: 600;
    border-left-color: var(--accent);
  }}
  .diff-ctx {{ color: var(--text-dim); }}

  /* ── WORD-LEVEL HIGHLIGHTS — the key feature ── */
  mark.wadd {{
    background: var(--wadd-bg);
    color: #7ee787;
    border-radius: 3px;
    padding: 1px 3px;
    margin: 0 1px;
    border-bottom: 2px solid var(--wadd-border);
    text-decoration: none;
  }}
  mark.wdel {{
    background: var(--wdel-bg);
    color: #ffa198;
    border-radius: 3px;
    padding: 1px 3px;
    margin: 0 1px;
    border-bottom: 2px solid var(--wdel-border);
    text-decoration: none;
  }}

  /* ── Footer ── */
  .footer-info {{
    margin-top: 20px;
    padding: 14px 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 13px;
    color: var(--text-dim);
  }}
  .footer-info strong {{ color: var(--text); }}
</style>
</head>
<body>
<div class="dashboard">

  <div class="header">
    <div class="header-left">
      <div class="header-icon">⌥</div>
      <div>
        {repo_name_html}
        <span class="branch-pill">{escape(info["branch"])}</span>
      </div>
    </div>
    <div class="header-right">
      Generated {now}<br>
      <button class="refresh-btn" onclick="window.location.reload()">↻ Refresh</button>
    </div>
  </div>

  <div class="stats-row">
    {stats_row_html}
  </div>

  <div class="panels">
    <div class="panel">
      <div class="panel-header">📋 Working Tree Status</div>
      <div class="panel-body">
        {file_status_html}
      </div>
    </div>
    <div class="panel">
      <div class="panel-header">📜 Recent Commits {commits_header_extra}</div>
      <div class="panel-body">
        {commits_html}
      </div>
    </div>
  </div>

  {diff_section}

  <div class="footer-info">
    <strong>Remote:</strong> {escape(info["remote_url"])}
    &nbsp;&nbsp;·&nbsp;&nbsp;
    <strong>Tracking:</strong> {escape(info["tracking"] or "none")}
    &nbsp;&nbsp;·&nbsp;&nbsp;
    <strong>Path:</strong> {escape(str(repo_path))}
  </div>

</div>
</body>
</html>'''

    return html


# ─── Main ───────────────────────────────────────────────────────────

HELP_TEXT = __doc__.strip()


def parse_args(argv):
    """Parse command-line arguments. Returns (range_spec, path)."""
    args = argv[1:]  # skip script name

    if not args:
        return None, "."

    if args[0] in ("--help", "-h"):
        print(HELP_TEXT)
        sys.exit(0)

    range_spec = None
    path = "."

    if args[0] == "--range":
        if len(args) < 2:
            print("Error: --range requires an argument (e.g. HEAD~3..HEAD)")
            sys.exit(1)
        range_spec = args[1]
        # Normalize open-ended: "HEAD~3.." → "HEAD~3"
        if range_spec.endswith(".."):
            range_spec = range_spec[:-2]
        if len(args) > 2:
            path = args[2]
    else:
        path = args[0]

    return range_spec, os.path.abspath(path)


def main():
    range_spec, start_path = parse_args(sys.argv)
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
