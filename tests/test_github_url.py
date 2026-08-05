"""
derive_github_url — the pure GitHub-URL helper extracted in Phase 2
(previously inline in repo collection; FINDINGS entry 1).

These are plain-string unit tests: no repo, no network. The integration
side — RepoSnapshot.github_url, which derives from remote_url — is covered
by the Phase 1 suite, which pins the negative case (local filesystem
remote -> github_url == "").
"""

import pytest


@pytest.mark.parametrize(
    "remote_url,expected",
    [
        # SSH form, trailing .git rewritten and stripped
        ("git@github.com:user/repo.git", "https://github.com/user/repo"),
        # HTTPS form, trailing .git stripped
        ("https://github.com/user/repo.git", "https://github.com/user/repo"),
        # Already-clean HTTPS URL passes through unchanged
        ("https://github.com/user/repo", "https://github.com/user/repo"),
        # SSH form without .git suffix
        ("git@github.com:user/repo", "https://github.com/user/repo"),
        # Non-GitHub remotes yield no URL
        ("git@gitlab.com:user/repo.git", ""),
        # The placeholder RepoSnapshot.from_repo uses with no origin
        ("No remote", ""),
    ],
)
def test_derive_github_url(gd, remote_url, expected):
    assert gd.derive_github_url(remote_url) == expected
