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
from dataclasses import dataclass, field
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
        # Capture bytes and decode by hand. git output is not guaranteed
        # UTF-8: a NUL-free binary file (some PDFs) slips past git's binary
        # sniff and gets text-diffed raw — strict decoding would crash
        # inside subprocess.run, past our except. And subprocess text mode
        # would also apply universal-newline translation, turning embedded
        # \r into \n and inflating such a diff's line count.
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            timeout=30
        )
        return result.stdout.decode("utf-8", errors="replace").strip()
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


# ─── Repo Snapshot ──────────────────────────────────────────────────

# One log line, as `--format=%h\t%H\t%s\t%ar` emits it.
LOG_FORMAT = "%h\t%H\t%s\t%ar"


@dataclass(frozen=True)
class Commit:
    """One commit, exactly as a commit row shows it."""

    hash: str = ""        # abbreviated — the displayed hash
    full_hash: str = ""   # full — GitHub links, and unpushed/behind membership
    msg: str = ""         # subject line
    date: str = ""        # git's %ar relative date ("3 days ago")


def _parse_log(log_output):
    """Turn LOG_FORMAT output into Commits, skipping any malformed line."""
    commits = []
    for line in (log_output.splitlines() if log_output else []):
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append(Commit(hash=parts[0], full_hash=parts[1],
                                  msg=parts[2], date=parts[3]))
    return commits


@dataclass(frozen=True)
class RepoSnapshot:
    """Everything the dashboard knows about a repository at one instant.

    This is the renderers' whole interface to git: every panel takes a
    snapshot and reads named fields off it, so a test can build one
    literally — no repository on disk — and render from it. RepoSnapshot
    itself runs no git; from_repo is the single adapter that does.

    Frozen, because a snapshot is a fact about a moment. Derive a variant
    with dataclasses.replace (the golden tests pin generated_at and the
    %ar relative dates that way).

    Every field defaults to its "nothing there" value, so a literal
    snapshot only spells out the facts the test is about. from_repo
    supplies the display fallbacks ("detached", "No remote") that a live
    repo needs and an empty snapshot doesn't.
    """

    # When the snapshot was taken — the header's "Generated ..." stamp.
    # Carried here rather than read from the clock at render time, which
    # is what keeps generate_html pure.
    generated_at: datetime = field(default_factory=datetime.now)

    # Identity
    repo_name: str = ""
    branch: str = ""
    remote_url: str = ""

    # Upstream: the configured ref, whether it still resolves, and the
    # distance to it. See from_repo for why "gone" is its own field.
    tracking: str = ""
    tracking_gone: bool = False
    ahead: int = 0
    behind: int = 0
    unpushed_hashes: set[str] = field(default_factory=set)  # full hashes

    # Default branch — the bottom "main" card. ref may be origin/main or
    # local main; see find_default_ref.
    default_branch: str = ""
    default_ref: str = ""
    behind_main_hashes: set[str] = field(default_factory=set)  # full hashes
    main_recent_commits: list[Commit] = field(default_factory=list)

    # Working tree
    stash_count: int = 0
    staged_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    staged_status: str = ""    # raw `diff --cached --name-status` text
    modified_status: str = ""  # raw `diff --name-status` text

    # Branch history
    recent_commits: list[Commit] = field(default_factory=list)

    @property
    def github_url(self):
        """Browsable GitHub URL, or "" for a non-GitHub remote."""
        return derive_github_url(self.remote_url)

    @property
    def show_main_card(self):
        """True when HEAD is somewhere other than a resolvable default branch —
        the only condition under which the bottom card has anything to say."""
        return bool(self.default_ref and self.default_branch != self.branch)

    @property
    def behind_main(self):
        return len(self.behind_main_hashes)

    @classmethod
    def from_repo(cls, repo_path):
        """Take a snapshot of a repository. The only place git is run for
        repo metadata."""
        branch = run_git(["branch", "--show-current"], repo_path) or "detached"
        remote_url = run_git(["remote", "get-url", "origin"], repo_path) or "No remote"
        tracking = run_git(
            ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
            repo_path
        ) or ""

        # Fetch quietly for accurate ahead/behind
        run_git(["fetch", "--quiet"], repo_path)

        # A branch can be *configured* to track a ref that no longer resolves —
        # remote branch deleted and pruned, say. rev-list against a gone ref
        # exits non-zero, run_git returns "", and 0/0 would render as a false
        # green "In sync". Detect the state explicitly (after the fetch, which
        # may prune or restore the ref) and report it instead.
        tracking_gone = bool(tracking) and not run_git(
            ["rev-parse", "--verify", "--quiet", tracking], repo_path
        )

        ahead = behind = 0
        if tracking and not tracking_gone:
            ahead_out = run_git(["rev-list", "--count", f"{tracking}..HEAD"], repo_path)
            behind_out = run_git(["rev-list", "--count", f"HEAD..{tracking}"], repo_path)
            ahead = int(ahead_out) if ahead_out.isdigit() else 0
            behind = int(behind_out) if behind_out.isdigit() else 0

        # Unpushed commit hashes (full hashes for reliable comparison)
        unpushed_hashes = set()
        if tracking_gone:
            # The upstream ref is gone, so no local commit can be on it —
            # every commit counts as unpushed.
            all_hashes = run_git(["rev-list", "HEAD"], repo_path)
            if all_hashes:
                unpushed_hashes = set(all_hashes.splitlines())
        elif tracking:
            unpushed = run_git(["rev-list", f"{tracking}..HEAD"], repo_path)
            if unpushed:
                unpushed_hashes = set(unpushed.splitlines())

        default_name, default_ref = find_default_ref(repo_path)
        show_main_card = bool(default_ref and default_name != branch)

        main_recent_commits = []
        behind_main_hashes = set()
        if show_main_card:
            behind_main_out = run_git(["rev-list", f"HEAD..{default_ref}"], repo_path)
            if behind_main_out:
                behind_main_hashes = set(behind_main_out.splitlines())
            # Main isn't the focus on a feature branch — show all the "new on
            # main" commits we can fit (up to 12), but cap "already in my
            # branch" context at 6 entries. If there are >12 behind commits,
            # the badge still shows the true count even though the list is
            # capped.
            IN_SYNC_CAP = 6
            in_sync_seen = 0
            for c in _parse_log(run_git(
                    ["log", "-12", f"--format={LOG_FORMAT}", default_ref], repo_path)):
                if c.full_hash not in behind_main_hashes:
                    in_sync_seen += 1
                    if in_sync_seen > IN_SYNC_CAP:
                        break
                main_recent_commits.append(c)

        # Recent commits (include full hash for GitHub links). On a feature
        # branch we only show commits unique to the branch — anything already
        # on main belongs in the bottom "main" card, not duplicated here.
        if show_main_card:
            log = run_git(
                ["log", "-12", f"--format={LOG_FORMAT}", f"{default_ref}..HEAD"],
                repo_path,
            )
        else:
            log = run_git(["log", "--oneline", "-12", f"--format={LOG_FORMAT}"],
                          repo_path)

        stash = run_git(["stash", "list"], repo_path)
        staged = run_git(["diff", "--cached", "--name-only"], repo_path)
        modified = run_git(["diff", "--name-only"], repo_path)
        untracked = run_git(["ls-files", "--others", "--exclude-standard"], repo_path)

        return cls(
            repo_name=os.path.basename(
                run_git(["rev-parse", "--show-toplevel"], repo_path) or repo_path
            ),
            branch=branch,
            remote_url=remote_url,
            tracking=tracking,
            tracking_gone=tracking_gone,
            ahead=ahead,
            behind=behind,
            unpushed_hashes=unpushed_hashes,
            default_branch=default_name,
            default_ref=default_ref,
            behind_main_hashes=behind_main_hashes,
            main_recent_commits=main_recent_commits,
            stash_count=len(stash.splitlines()) if stash else 0,
            staged_files=staged.splitlines() if staged else [],
            modified_files=modified.splitlines() if modified else [],
            untracked_files=untracked.splitlines() if untracked else [],
            staged_status=run_git(["diff", "--cached", "--name-status"], repo_path),
            modified_status=run_git(["diff", "--name-status"], repo_path),
            recent_commits=_parse_log(log),
        )


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

# Display guards for pathological diffs. Git's binary sniff only checks the
# first 8000 bytes for a NUL, so a NUL-free binary file (some PDFs) gets
# text-diffed raw — thousands of lines of stream garbage. And this dashboard
# is for reviewing small diffs and staying aware of changes, not reading
# bulk moves; anything longer belongs back at the command line. The guards
# apply to display only — compute_state_hash hashes the full raw diff.
DIFF_FILE_MAX_LINES = 250    # beyond this, a file's diff is truncated...
DIFF_FILE_SHOWN_LINES = 200  # ...to this many lines (never hides < 50)
BINARY_WEIRD_CHAR_RATIO = 0.10

# Header icon for files whose diff isn't rendered (git-detected binary or
# suppressed as apparent binary); files with a rendered diff get a
# disclosure triangle instead.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic",
                    ".ico", ".bmp", ".tiff", ".tif", ".svg"}

# U+FFFD (run_git's errors="replace" output) and C0 controls other than
# tab/newline. Latin-1 text decoded as UTF-8 stays under a few percent —
# one U+FFFD per accented char; binary content lands far above 10%.
_WEIRD_DIFF_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f�]")


def _looks_binary(text):
    """True if a file's diff body reads as binary rather than text."""
    if "\x00" in text:
        return True
    return len(_WEIRD_DIFF_CHARS.findall(text)) > len(text) * BINARY_WEIRD_CHAR_RATIO


def parse_and_render_diff(diff_text, diff_label):
    """
    Parse git diff output and render as HTML with word-level highlighting.

    Splits on \\n only — git's own line definition. splitlines() would also
    split on control characters embedded inside a git line (\\x0b, \\x0c,
    \\x1e, ...), multiplying binary content into thousands of extra lines
    that masquerade as context.
    """
    if not diff_text:
        return ""

    html_parts = []
    hunk_removed = []  # Buffer for consecutive removed lines
    hunk_added = []    # Buffer for consecutive added lines

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

    # Group the diff into per-file segments so each file can be classified
    # (binary? oversize?) before anything is rendered.
    segments = []  # (header_line, body_lines) per "diff --git" section
    for raw_line in diff_text.split("\n"):
        if raw_line.startswith("diff --git"):
            segments.append((raw_line, []))
        elif segments:
            segments[-1][1].append(raw_line)
    if segments and segments[-1][1] and segments[-1][1][-1] == "":
        segments[-1][1].pop()  # trailing newline artifact of split("\n")

    for header_line, body_lines in segments:
        match = re.search(r'b/(.+)$', header_line)
        current_file = match.group(1) if match else header_line

        # Displayed lines start at the first hunk header; anything before it
        # (index, ---/+++, mode lines) is metadata and is not rendered.
        first_hunk = next(
            (i for i, l in enumerate(body_lines) if l.startswith("@@")), None
        )
        displayed = body_lines[first_hunk:] if first_hunk is not None else []
        is_binary = bool(displayed) and _looks_binary("\n".join(displayed))

        # The disclosure triangle marks headers with an actual diff to
        # expand; files with nothing readable underneath keep a type icon.
        # Decided by what the renderer shows, not the extension: a
        # text-diffed .svg gets the triangle, a text-diffed-but-suppressed
        # .pdf does not.
        if displayed and not is_binary:
            icon = '<span class="diff-disclosure"></span>'
        elif os.path.splitext(current_file)[1].lower() in IMAGE_EXTENSIONS:
            icon = '<span class="diff-file-icon">🖼️</span>'
        else:
            icon = '<span class="diff-file-icon">📄</span>'

        header = (
            f'<details class="diff-file" open>'
            f'<summary class="diff-file-header">'
            f'{icon} {escape(current_file)}'
            f'<span class="diff-label-pill pill-{diff_label}">{diff_label}</span>'
            f'</summary>'
        )

        if not displayed:
            # No hunks (git-detected binary, mode change): header only —
            # an expandable empty box would advertise a pointless click.
            html_parts.append(header + "</details>")
            continue

        html_parts.append(header + '<div class="diff-content">')

        suppressed = 0
        if is_binary:
            html_parts.append(
                f'<div class="diff-line diff-note">Apparent binary file'
                f' — diff suppressed ({len(displayed):,} lines)</div>'
            )
            displayed = []
        elif len(displayed) > DIFF_FILE_MAX_LINES:
            suppressed = len(displayed) - DIFF_FILE_SHOWN_LINES
            displayed = displayed[:DIFF_FILE_SHOWN_LINES]

        in_hunk = False
        for raw_line in displayed:
            if raw_line.startswith("@@"):
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

        if suppressed:
            command = {"staged": "git diff --cached",
                       "unstaged": "git diff"}.get(diff_label, "git diff")
            html_parts.append(
                f'<div class="diff-line diff-note">… {suppressed:,} more lines'
                f' — run {command} to see the rest</div>'
            )
        html_parts.append("</div></details>")

    return "\n".join(html_parts)


# ─── Diff Sources ───────────────────────────────────────────────────

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


def _plural(n, noun):
    """"1 file" / "2 files" — the only pluralization the dashboard needs."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class DiffView:
    """Everything the renderer needs to know about the diffs it is showing,
    with no trace of where they came from.

    Two adapters satisfy it — WorkingTreeDiffs (staged + unstaged) and
    RangeDiffs (one section for a commit range). They own every git
    invocation and the whole "is this a range?" question, which is why
    generate_html can be pure and why tests can hand-build a view and
    render it with no repository on disk.

    Fields (all optional; the defaults describe "nothing to show"):

      label          panel-header suffix when there is something to show,
                     HTML-escaped at render time.
      empty_label    header suffix when sections is empty; "" drops the
                     dash entirely.
      empty_message  panel body text when sections is empty.
      sections       (section_label, diff_html) pairs in display order,
                     each rendered with a divider between it and the one
                     before. section_label is emitted as-is, so adapters
                     must supply markup-safe text. Empty means the panel
                     falls back to the empty state.
      stat_cards     the leading stat cards, as (title, css_class, count,
                     display) — count drives the "zero" styling, display
                     is the text shown. The Stashes card is appended by
                     the renderer; it comes from the repo, not the diff.
    """

    def __init__(self, label="", empty_label="", empty_message="",
                 sections=(), stat_cards=()):
        self.label = label
        self.empty_label = empty_label
        self.empty_message = empty_message
        self.sections = list(sections)
        self.stat_cards = list(stat_cards)


class WorkingTreeDiffs(DiffView):
    """Uncommitted work: what is staged and what is not."""

    def __init__(self, repo_path, snap):
        staged_count = len(snap.staged_files)
        modified_count = len(snap.modified_files)
        untracked_count = len(snap.untracked_files)

        sections = []
        staged_html = parse_and_render_diff(
            run_git(["diff", "--cached"], repo_path), "staged")
        if staged_html:
            sections.append(
                (f'Staged Changes ({_plural(staged_count, "file")})',
                 staged_html))
        unstaged_html = parse_and_render_diff(
            run_git(["diff"], repo_path), "unstaged")
        if unstaged_html:
            sections.append(
                (f'Unstaged Changes ({_plural(modified_count, "file")})',
                 unstaged_html))

        super().__init__(
            label="Word-Level Change Detection",
            empty_message="No uncommitted changes to diff",
            sections=sections,
            stat_cards=[
                ("Staged", "staged", staged_count, str(staged_count)),
                ("Modified", "modified", modified_count, str(modified_count)),
                ("Untracked", "untracked", untracked_count,
                 str(untracked_count)),
            ],
        )


class RangeDiffs(DiffView):
    """A commit range: one section covering the whole span."""

    def __init__(self, repo_path, range_spec):
        # git diff takes both spellings the same way; only the label differs.
        # A two-dot spec is committed changes only, a bare ref runs from that
        # ref up to the working tree.
        label = range_spec if ".." in range_spec else f"{range_spec}..working tree"
        diff_html = parse_and_render_diff(
            run_git(["diff", range_spec], repo_path), "range")
        files, insertions, deletions = parse_diff_stat(
            run_git(["diff", "--stat", range_spec], repo_path))

        sections = []
        if diff_html:
            sections.append(
                (f'{_plural(files, "file")} changed,'
                 f' +{insertions} −{deletions}', diff_html))

        super().__init__(
            label=label,
            empty_label=label,
            empty_message="No changes in this range",
            sections=sections,
            stat_cards=[
                ("Files Changed", "modified", files, str(files)),
                ("Insertions", "staged", insertions, f"+{insertions}"),
                ("Deletions", "untracked", deletions, f"−{deletions}"),
            ],
        )


def build_diff_view(repo_path, range_spec, snap):
    """Pick the adapter for this run. The only place the range-vs-working-tree
    question is asked."""
    if range_spec is not None:
        return RangeDiffs(repo_path, range_spec)
    return WorkingTreeDiffs(repo_path, snap)


# ─── HTML Generation ────────────────────────────────────────────────

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
  .diff-disclosure {
    flex-shrink: 0;
    width: 1.2em;
    text-align: center;
    color: var(--text-dim);
  }
  .diff-disclosure::before {
    content: '▸';
    display: inline-block;
    transition: transform 0.15s ease;
  }
  .diff-file[open] > .diff-file-header .diff-disclosure::before {
    transform: rotate(90deg);
  }
  .diff-fold-controls { margin-left: auto; display: flex; gap: 6px; }
  .diff-fold {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 8px;
    border-radius: 4px;
    background: transparent;
    color: var(--text-dim);
    border: 1px solid var(--border);
    cursor: pointer;
  }
  .diff-fold:hover { color: var(--text-bright); border-color: var(--accent); }
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
  .diff-note { color: var(--text-dim); font-style: italic; padding: 6px 14px; }

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


def _render_file_status(snap, live, repo_path):
    """Working Tree Status panel body — staged/modified/untracked rows."""
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
    for line in (snap.staged_status.splitlines() if snap.staged_status else []):
        label, cls = status_badge(line, "staged", "badge-staged")
        attr = open_attr(line.split("\t")[-1])
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge {cls}">{label}</span> {escape(line)}</div>\n'
    for line in (snap.modified_status.splitlines() if snap.modified_status else []):
        label, cls = status_badge(line, "modified", "badge-modified")
        attr = open_attr(line.split("\t")[-1])
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge {cls}">{label}</span> {escape(line)}</div>\n'
    for f in snap.untracked_files:
        attr = open_attr(f)
        file_status_html += f'<div class="file-entry"{attr}><span class="file-badge badge-untracked">untracked</span> {escape(f)}</div>\n'

    if not file_status_html:
        file_status_html = '<div class="panel-empty">✓ Working tree clean</div>'
    return file_status_html


def _render_commits(snap, github_url):
    """Branch commit rows — unpushed highlighted, pushed linked to GitHub."""
    commits_html = ""
    for c in snap.recent_commits:
        is_unpushed = c.full_hash in snap.unpushed_hashes
        entry_class = "commit-entry commit-unpushed" if is_unpushed else "commit-entry"

        if is_unpushed or not github_url:
            hash_html = f'<span class="commit-hash">{escape(c.hash)}</span>'
        else:
            commit_url = f'{github_url}/commit/{c.full_hash}'
            hash_html = f'<a class="commit-hash commit-link" href="{escape(commit_url)}" target="_blank">{escape(c.hash)}</a>'

        commits_html += (
            f'<div class="{entry_class}">'
            f'{hash_html}'
            f'<span class="commit-msg">{escape(c.msg)}</span>'
            f'<span class="commit-date">{escape(c.date)}</span>'
            f'</div>\n'
        )
    return commits_html


def _render_branch_panel(snap, commits_html):
    """Top-right panel: current-branch commits plus a sync-status badge."""
    ahead, behind = snap.ahead, snap.behind
    sync_parts = []
    if snap.tracking_gone:
        # Upstream configured but its ref is gone — ahead/behind are
        # uncomputable, and 0/0 must not read as "In sync". Reuses the
        # yellow sync-ahead style (matches the all-commits-unpushed
        # highlighting) so DASHBOARD_CSS — embedded in the goldens —
        # doesn't change.
        sync_parts.append('<span class="sync-badge sync-ahead">Upstream gone</span>')
    elif ahead > 0 or behind > 0:
        if ahead > 0:
            sync_parts.append(f'<span class="sync-badge sync-ahead">↑ {ahead} unpushed</span>')
        if behind > 0:
            sync_parts.append(f'<span class="sync-badge sync-behind">↓ {behind} behind</span>')
    elif snap.tracking:
        sync_parts.append('<span class="sync-badge sync-ok">In sync</span>')
    else:
        sync_parts.append('<span class="sync-badge sync-notrack">No remote</span>')
    commits_header_extra = " ".join(sync_parts)

    show_main = snap.show_main_card
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
    return branch_panel_html


def _render_main_panel(snap, github_url):
    """Bottom-right panel: HEAD versus the default branch."""
    show_main = snap.show_main_card
    main_panel_html = ""
    if show_main:
        default_name = snap.default_branch
        behind = snap.behind_main
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
        behind_hashes = snap.behind_main_hashes
        for c in snap.main_recent_commits:
            is_behind = c.full_hash in behind_hashes
            entry_class = (
                "commit-entry commit-behind-main" if is_behind else "commit-entry"
            )
            if github_url:
                commit_url = f'{github_url}/commit/{c.full_hash}'
                hash_html = (
                    f'<a class="commit-hash commit-link" '
                    f'href="{escape(commit_url)}" target="_blank">'
                    f'{escape(c.hash)}</a>'
                )
            else:
                hash_html = f'<span class="commit-hash">{escape(c.hash)}</span>'
            main_commits_html += (
                f'<div class="{entry_class}">'
                f'{hash_html}'
                f'<span class="commit-msg">{escape(c.msg)}</span>'
                f'<span class="commit-date">{escape(c.date)}</span>'
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
    return main_panel_html


def _render_stats_row(snap, view):
    """Compact stat cards — the view supplies the leading three, the repo
    supplies the Stashes card that follows them."""
    def zero_class(n):
        return " zero" if n == 0 else ""

    cards = view.stat_cards + [
        ("Stashes", "stash", snap.stash_count, str(snap.stash_count)),
    ]
    return "".join(f'''
    <div class="stat-card {css_class}">
      <div class="stat-label">{title}</div>
      <div class="stat-value{zero_class(count)}">{display}</div>
    </div>''' for title, css_class, count, display in cards)


def _render_diff_section(view):
    """Full-width Diff Viewer panel — one section per entry in the view."""
    if not view.sections:
        # Nothing to fold, so no fold controls; and the working-tree view
        # leaves empty_label blank, which drops the header's dash too.
        suffix = f" — {escape(view.empty_label)}" if view.empty_label else ""
        return (
            '<div class="panel panel-full">'
            f'<div class="panel-header">🔍 Diff Viewer{suffix}</div>'
            f'<div class="panel-body"><div class="panel-empty">{view.empty_message}</div></div>'
            '</div>'
        )

    fold = "document.querySelectorAll('details.diff-file').forEach(d=>d.open="
    fold_controls = (
        '<span class="diff-fold-controls">'
        f'<button class="diff-fold" onclick="{fold}true)">Expand all</button>'
        f'<button class="diff-fold" onclick="{fold}false)">Collapse all</button>'
        '</span>'
    )
    diff_section = f'<div class="panel panel-full"><div class="panel-header">🔍 Diff Viewer — {escape(view.label)}{fold_controls}</div>\n'
    diff_section += '<div class="panel-body diff-scroll">\n'
    for i, (section_label, section_html) in enumerate(view.sections):
        if i:
            diff_section += '<div class="diff-divider"></div>\n'
        diff_section += f'<div class="diff-section-label">{section_label}</div>\n'
        diff_section += section_html + "\n"
    diff_section += '</div></div>\n'
    return diff_section


def generate_html(snap, repo_path, view, live=False):
    """Generate the complete dashboard HTML.

    Pure: every repo fact comes in through `snap` (a RepoSnapshot) and every
    diff and stat through `view` (a DiffView), so this runs no git, reads no
    clock, and knows nothing about range vs working-tree mode. repo_path is
    display material — the header path and the reveal links.
    """
    github_url = snap.github_url

    # Assemble the page fragments, then fill the page template.
    file_status_html = _render_file_status(snap, live, repo_path)
    commits_html = _render_commits(snap, github_url)
    branch_panel_html = _render_branch_panel(snap, commits_html)
    main_panel_html = _render_main_panel(snap, github_url)
    right_column_html = (
        f'<div class="right-stack">{branch_panel_html}{main_panel_html}</div>'
    )
    diff_section = _render_diff_section(view)
    stats_row_html = _render_stats_row(snap, view)

    now = snap.generated_at.strftime("%B %d, %Y at %I:%M:%S %p")

    # Repo name — linked to GitHub if available
    if github_url:
        repo_name_html = f'<a class="repo-name" href="{escape(github_url)}" target="_blank">{escape(snap.repo_name)}</a>'
    else:
        repo_name_html = f'<div class="repo-name">{escape(snap.repo_name)}</div>'

    # Branch pill — just the branch name, unless it tracks a non-obvious
    # upstream (a fork's upstream, a differently-named remote branch, …), in
    # which case show "branch → upstream" so the mismatch is visible.
    branch = snap.branch
    tracking = snap.tracking
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
        title=escape(snap.repo_name),
        repo_name=repo_name_html,
        branch_label=escape(branch_label),
        refresh=refresh_html,
        now=now,
        remote=escape(snap.remote_url),
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
    main moves in a parallel terminal session in a no-remote repo. Note the
    remote-tracking refs only move when a fetch runs, and fetch is deliberately
    not on an idle timer — see README "When remote changes appear".

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
        snap = RepoSnapshot.from_repo(self.repo_path)
        self._rebuild_allowed_paths(snap)
        view = build_diff_view(self.repo_path, self.range_spec, snap)
        html = generate_html(snap, self.repo_path, view, live=True)
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

    def _rebuild_allowed_paths(self, snap):
        """Refresh the set of paths /open may reveal: the repo root plus
        every changed file that currently exists on disk."""
        allowed = {os.path.realpath(self.repo_path)}
        for rel in (snap.staged_files + snap.modified_files
                    + snap.untracked_files):
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
    snap = RepoSnapshot.from_repo(repo_path)
    view = build_diff_view(repo_path, range_spec, snap)
    html = generate_html(snap, repo_path, view)

    safe_name = re.sub(r'[^\w\-]', '-', snap.repo_name).strip('-')
    output_path = f"/tmp/{safe_name}-git-dashboard.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard: {output_path}")
    webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    main()
