# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Committing

- `__pycache__/` is gitignored. It appears whenever the script is
  byte-compiled (e.g. `python3 -m py_compile git-dashboard.py`). Before making
  a commit, check whether a `__pycache__/` directory exists in the working
  tree; if it does, offer to remove it (`rm -rf __pycache__`).
