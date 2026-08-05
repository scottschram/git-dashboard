"""
Group A: parse_args — CLI argument parsing.

parse_args takes the full argv (program name at index 0) and returns
(range_spec, path, once).
"""

import os

import pytest


def test_module_loads_without_side_effects(gd):
    # The brief asks us to confirm loading the module is side-effect-free:
    # it only defines functions/classes/constants (main() is guarded by
    # __name__ == "__main__", which importlib does not trigger). If loading
    # started a server or opened a browser, the suite itself would hang.
    for name in (
        "run_git",
        "find_default_ref",
        "compute_state_hash",
        "parse_args",
        "generate_html",
    ):
        assert callable(getattr(gd, name))
    assert callable(gd.RepoSnapshot.from_repo)
    assert isinstance(gd.HELP_TEXT, str) and gd.HELP_TEXT


def test_no_args_watch_mode(gd):
    # Characterization: the no-arg branch returns the *bare* "." — every
    # other branch abspaths the path before returning. Harmless in practice
    # (main() re-abspaths), pinned as-is per the test brief.
    assert gd.parse_args(["prog"]) == (None, ".", False)


def test_once_flag(gd):
    range_spec, path, once = gd.parse_args(["prog", "--once"])
    assert range_spec is None
    assert once is True
    # Unlike the no-arg branch, this branch abspaths the default ".".
    assert path == os.path.abspath(".")


def test_positional_path_is_abspathed(gd):
    range_spec, path, once = gd.parse_args(["prog", "somepath"])
    assert range_spec is None
    assert once is False
    assert path == os.path.abspath("somepath")


def test_range_two_dot_sets_spec_and_forces_once(gd):
    range_spec, path, once = gd.parse_args(["prog", "--range", "HEAD~3..HEAD"])
    assert range_spec == "HEAD~3..HEAD"
    assert once is True
    assert path == os.path.abspath(".")


def test_range_open_ended_is_normalized(gd):
    range_spec, _, once = gd.parse_args(["prog", "--range", "HEAD~3.."])
    assert range_spec == "HEAD~3"
    assert once is True


def test_range_with_explicit_path(gd):
    range_spec, path, once = gd.parse_args(
        ["prog", "--range", "HEAD~1..HEAD", "somewhere"]
    )
    assert range_spec == "HEAD~1..HEAD"
    assert path == os.path.abspath("somewhere")
    assert once is True


def test_once_can_appear_after_other_args(gd):
    _, path, once = gd.parse_args(["prog", "somepath", "--once"])
    assert once is True
    assert path == os.path.abspath("somepath")


def test_range_missing_argument_exits_nonzero(gd, capsys):
    with pytest.raises(SystemExit) as excinfo:
        gd.parse_args(["prog", "--range"])
    assert excinfo.value.code == 1
    assert "--range requires an argument" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_help_text_and_exits_zero(gd, capsys, flag):
    with pytest.raises(SystemExit) as excinfo:
        gd.parse_args(["prog", flag])
    assert excinfo.value.code == 0
    assert gd.HELP_TEXT in capsys.readouterr().out
