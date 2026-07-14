"""
Group A: the pure word-level diff engine and diff parsing/rendering.

tokenize_for_diff, compute_similarity, word_level_diff, pair_changed_lines,
parse_diff_stat, parse_and_render_diff. No git involved.
"""

import re

import pytest


def strip_marks(html_text):
    """Remove <mark ...>/</mark> tags so escaping can be checked in isolation."""
    return re.sub(r"</?mark[^>]*>", "", html_text)


# ─── tokenize_for_diff ──────────────────────────────────────────────

def test_tokenize_empty_string(gd):
    assert gd.tokenize_for_diff("") == []


def test_tokenize_whitespace_only_is_one_token(gd):
    assert gd.tokenize_for_diff("  \t ") == ["  \t "]


def test_tokenize_splits_punctuation_from_word(gd):
    assert gd.tokenize_for_diff('"Hello,') == ['"', "Hello", ","]


def test_tokenize_adjacent_punctuation_clusters(gd):
    # Characterization: consecutive punctuation stays one token (',"' is
    # a single cluster, not two).
    assert gd.tokenize_for_diff('Hello,"') == ["Hello", ',"']


def test_tokenize_preserves_internal_whitespace_run(gd):
    assert gd.tokenize_for_diff("a   b") == ["a", "   ", "b"]


def test_tokenize_unicode_word_chars_stay_together(gd):
    # \w matches unicode letters by default, so accented words are one token.
    assert gd.tokenize_for_diff("café über") == ["café", " ", "über"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "hello world",
        '"Hello," she said — twice.',
        "café → naïve",
        "a\t b\n  c",
        "x_1 = f(y); // done",
    ],
)
def test_tokenize_round_trips(gd, text):
    # Invariant: tokens partition the input — joining them recovers it exactly.
    assert "".join(gd.tokenize_for_diff(text)) == text


# ─── compute_similarity ─────────────────────────────────────────────

def test_similarity_identical_lines(gd):
    assert gd.compute_similarity("same line", "same line") == 1.0


def test_similarity_both_empty(gd):
    # Characterization: difflib defines the ratio of two empty sequences
    # as 1.0; pin that so a difflib behavior change would surface here.
    assert gd.compute_similarity("", "") == 1.0


def test_similarity_one_empty(gd):
    assert gd.compute_similarity("", "abc") == 0.0
    assert gd.compute_similarity("abc", "") == 0.0


def test_similarity_partial_overlap(gd):
    ratio = gd.compute_similarity("abcd", "abXd")
    assert 0.0 < ratio < 1.0
    assert ratio == pytest.approx(0.75)  # 2 * 3 matches / 8 total chars


# ─── word_level_diff ────────────────────────────────────────────────

def test_word_diff_identical_lines_have_no_marks(gd):
    old_html, new_html = gd.word_level_diff("a & b", "a & b")
    assert "<mark" not in old_html
    assert "<mark" not in new_html
    assert old_html == new_html == "a &amp; b"


def test_word_diff_full_replace_marks_both_sides(gd):
    old_html, new_html = gd.word_level_diff("aaa", "zzz")
    assert old_html == '<mark class="wdel">aaa</mark>'
    assert new_html == '<mark class="wadd">zzz</mark>'


def test_word_diff_insert_only(gd):
    old_html, new_html = gd.word_level_diff("", "brand new")
    assert old_html == ""
    assert new_html == '<mark class="wadd">brand new</mark>'
    assert "wdel" not in new_html


def test_word_diff_delete_only(gd):
    old_html, new_html = gd.word_level_diff("gone now", "")
    assert old_html == '<mark class="wdel">gone now</mark>'
    assert new_html == ""


def test_word_diff_escapes_html_metacharacters(gd):
    old_html, new_html = gd.word_level_diff('x < 1 & "q"', 'x < 2 & "q"')
    for out in (old_html, new_html):
        assert "&lt;" in out
        assert "&amp;" in out
        assert "&quot;" in out
        # After removing the <mark> tags we added, no raw '<' from the
        # *input* may survive.
        assert "<" not in strip_marks(out)
    # The changed token is highlighted on both sides.
    assert '<mark class="wdel">1</mark>' in old_html
    assert '<mark class="wadd">2</mark>' in new_html


# ─── pair_changed_lines ─────────────────────────────────────────────

def test_pair_both_empty(gd):
    assert gd.pair_changed_lines([], []) == []


def test_pair_no_removals_all_pure_additions_in_order(gd):
    added = ["first", "second", "third"]
    assert gd.pair_changed_lines([], added) == [
        (None, "first"),
        (None, "second"),
        (None, "third"),
    ]


def test_pair_no_additions_all_pure_deletions_in_order(gd):
    removed = ["first", "second"]
    assert gd.pair_changed_lines(removed, []) == [
        ("first", None),
        ("second", None),
    ]


def test_pair_equal_counts_high_similarity_match_up(gd):
    removed = ["the quick brown fox jumps", "over the lazy dog sleeps"]
    added = ["the quick red fox jumps", "over the lazy cat sleeps"]
    assert gd.pair_changed_lines(removed, added) == [
        (removed[0], added[0]),
        (removed[1], added[1]),
    ]


def test_pair_nothing_above_threshold_removals_then_additions(gd):
    # Characterization (traced through the code): with no similarity above
    # the 0.4 threshold, matched_pairs is empty, so the output loop emits
    # every removal as (old, None) first, then the trailing while-loop emits
    # every addition as (None, new) — all removals, then all additions.
    removed = ["aaaa aaaa", "bbbb bbbb"]
    added = ["zzzz zzzz", "yyyy yyyy"]
    assert gd.pair_changed_lines(removed, added) == [
        ("aaaa aaaa", None),
        ("bbbb bbbb", None),
        (None, "zzzz zzzz"),
        (None, "yyyy yyyy"),
    ]


def test_pair_unmatched_addition_emitted_before_its_matched_neighbor(gd):
    # removed[0] best-matches added[1]; the unmatched added[0] must be
    # emitted first so output preserves the new-side order.
    removed = ["the quick brown fox jumps over"]
    added = ["zzzz qqqq vvvv kkkk", "the quick brown fox jumps over!"]
    assert gd.pair_changed_lines(removed, added) == [
        (None, added[0]),
        (removed[0], added[1]),
    ]


def test_pair_uneven_counts_three_removed_one_added(gd):
    removed = ["alpha beta gamma", "qqqq wwww", "zzzz xxxx"]
    added = ["alpha beta gamma delta"]
    assert gd.pair_changed_lines(removed, added) == [
        (removed[0], added[0]),
        (removed[1], None),
        (removed[2], None),
    ]


# ─── parse_diff_stat ────────────────────────────────────────────────

def test_diff_stat_empty(gd):
    assert gd.parse_diff_stat("") == (0, 0, 0)


def test_diff_stat_full_summary(gd):
    line = " 3 files changed, 10 insertions(+), 2 deletions(-)"
    assert gd.parse_diff_stat(line) == (3, 10, 2)


def test_diff_stat_insertions_only(gd):
    assert gd.parse_diff_stat(" 2 files changed, 5 insertions(+)") == (2, 5, 0)


def test_diff_stat_deletions_only(gd):
    assert gd.parse_diff_stat(" 1 file changed, 4 deletions(-)") == (1, 0, 4)


def test_diff_stat_singular_forms(gd):
    line = " 1 file changed, 1 insertion(+), 1 deletion(-)"
    assert gd.parse_diff_stat(line) == (1, 1, 1)


def test_diff_stat_uses_last_line_of_multiline_output(gd):
    # Characterization: real `git diff --stat` output has per-file lines
    # (full of digits) before the summary; only the last line is parsed.
    stat = (
        " README.md   | 10 +++++-----\n"
        " docs/x.md   |  2 ++\n"
        " 2 files changed, 7 insertions(+), 5 deletions(-)"
    )
    assert gd.parse_diff_stat(stat) == (2, 7, 5)


# ─── parse_and_render_diff ──────────────────────────────────────────
# Characterization-flavored: assert structure (which fragments appear, in
# what order), not exact bytes.

SINGLE_FILE_DIFF = """\
diff --git a/notes.txt b/notes.txt
index 83db48f..bf269f4 100644
--- a/notes.txt
+++ b/notes.txt
@@ -1,3 +1,3 @@
 context before
-hello world
+hello brave world
 context after
"""


def test_render_empty_diff_is_empty_string(gd):
    assert gd.parse_and_render_diff("", "unstaged") == ""


def test_render_single_file_hunk_structure(gd):
    out = gd.parse_and_render_diff(SINGLE_FILE_DIFF, "unstaged")

    # One file section, with header, filename, and the label pill.
    assert out.count('<details class="diff-file"') == 1
    assert 'class="diff-file-header"' in out
    assert "notes.txt" in out
    assert 'class="diff-label-pill pill-unstaged">unstaged<' in out
    assert out.rstrip().endswith("</div></details>")

    # The hunk header line survives as a diff-hunk div.
    assert "diff-hunk" in out
    assert "@@ -1,3 +1,3 @@" in out

    # One del/add pair with word-level highlighting: "brave " is the only
    # inserted token, so only a wadd mark appears, on the added line.
    assert out.count("diff-del") == 1
    assert out.count("diff-add") == 1
    assert '<mark class="wadd">brave </mark>' in out
    assert "wdel" not in out


def test_render_context_line_flushes_change_buffer(gd):
    # The -/+ pair sits between the two context lines, so its rendered lines
    # must land between them: the trailing context line forces the buffered
    # changes to flush before it is emitted.
    out = gd.parse_and_render_diff(SINGLE_FILE_DIFF, "unstaged")
    assert (
        out.index("context before")
        < out.index("diff-del")
        < out.index("diff-add")
        < out.index("context after")
    )


def test_render_skips_no_newline_marker(gd):
    diff = """\
diff --git a/a.txt b/a.txt
index 0000000..1111111 100644
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    out = gd.parse_and_render_diff(diff, "staged")
    assert "No newline" not in out
    assert "diff-del" in out and "diff-add" in out


def test_render_two_files_two_sections(gd):
    diff = """\
diff --git a/one.txt b/one.txt
--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-uno
+one
diff --git a/two.txt b/two.txt
--- a/two.txt
+++ b/two.txt
@@ -1 +1 @@
-dos
+two
"""
    out = gd.parse_and_render_diff(diff, "staged")
    assert out.count('<details class="diff-file"') == 2
    assert out.count("</div></details>") == 2
    assert "one.txt" in out and "two.txt" in out


# ─── parse_and_render_diff: pathological-diff guards ────────────────
# Motivating incident: a PDF with no NUL in its first 8000 bytes slipped
# git's binary sniff and was text-diffed — 11,566 lines of raw stream data
# that first crashed run_git's strict decode, then shipped the browser a
# 7.8 MB page. These guards live in the display layer only;
# compute_state_hash still sees the full raw diff.

def _deletion_diff(filename, lines):
    """A single-file whole-file-deletion diff, as git renders one."""
    return (
        f"diff --git a/{filename} b/{filename}\n"
        f"deleted file mode 100644\n"
        f"index 1111111..0000000\n"
        f"--- a/{filename}\n"
        f"+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n"
        + "\n".join(f"-{line}" for line in lines) + "\n"
    )


def test_render_collapses_diff_containing_nul(gd):
    # Any NUL means binary — git's own heuristic, minus the 8000-byte
    # window that let the PDF through. NULs survive errors="replace"
    # decoding (0x00 is valid UTF-8), so they reach the renderer.
    diff = _deletion_diff("doc.pdf", ["%PDF-1.4", "stream\x00data", "endstream"])
    out = gd.parse_and_render_diff(diff, "staged")

    assert "Apparent binary file" in out
    assert "diff-del" not in out
    # The file is still listed: header, name, and pill survive.
    assert out.count('<details class="diff-file"') == 1
    assert "doc.pdf" in out
    assert 'pill-staged' in out


def test_render_collapses_replacement_char_dense_diff(gd):
    # NUL-free binary: run_git's errors="replace" turns invalid bytes into
    # U+FFFD. Above 10% weird-char density the content was never text.
    lines = ["���� soup ���"] * 6
    out = gd.parse_and_render_diff(_deletion_diff("blob.bin", lines), "unstaged")

    assert "Apparent binary file" in out
    assert "diff-del" not in out


def test_render_keeps_text_with_sparse_replacement_chars(gd):
    # A Latin-1 text file decodes with a U+FFFD hole per accented char —
    # a few percent density. Still a readable, useful diff: must NOT collapse.
    lines = ["Agnus De�, qui tollis peccata mundi"] * 10
    out = gd.parse_and_render_diff(_deletion_diff("lyrics.txt", lines), "unstaged")

    assert "Apparent binary file" not in out
    assert out.count("diff-del") == 10


def test_render_truncates_oversize_diff(gd):
    # 300 deleted lines + 1 hunk header = 301 displayed lines, over the 250
    # cap: show the first 200 (hunk header + 199 content), then a stub that
    # points back at the command line for the rest.
    lines = [f"line {i} content" for i in range(300)]
    out = gd.parse_and_render_diff(_deletion_diff("big.txt", lines), "staged")

    assert "-line 198 content</div>" in out
    assert "-line 199 content</div>" not in out
    assert "101 more lines" in out
    assert "git diff --cached" in out  # staged label → staged command


def test_render_at_truncation_threshold_shows_everything(gd):
    # 249 content lines + hunk header = 250 displayed lines: exactly at the
    # cap, nothing is hidden — no cliff where a small diff loses its tail.
    lines = [f"line {i} content" for i in range(249)]
    out = gd.parse_and_render_diff(_deletion_diff("mid.txt", lines), "unstaged")

    assert "-line 248 content</div>" in out
    assert "more lines" not in out


def test_render_splits_on_newline_only(gd):
    # str.splitlines() also splits on \x0b, \x0c, \x85, etc. — control
    # characters that binary content embeds *inside* a git line. That
    # multiplied 13,300 git lines into 73,235 rendered lines, with the
    # fragments masquerading as context lines. Git defines lines by \n
    # alone; the renderer must match git.
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-alpha\x0bbeta gamma delta epsilon zeta\x85eta theta iota kappa\n"
        "+replacement line of ordinary text\n"
    )
    out = gd.parse_and_render_diff(diff, "unstaged")

    assert out.count("diff-del") == 1     # one git line → one rendered line
    assert "diff-ctx" not in out          # no fragments posing as context
    assert "kappa" in out                 # ...and nothing lost
