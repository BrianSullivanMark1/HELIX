"""GithubRepos against a scripted HTTP: host pinned, token never logged, compare direction flipped
once and correctly."""
from __future__ import annotations

import json

import pytest

from helix.adapters import github_fleet as gh
from helix.domain import constitution, fleet


class Http:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, headers, timeout_s):
        self.calls.append((url, headers))
        for needle, ans in self.answers.items():
            if needle in url:
                return ans
        return 404, ""


def commit_doc(sha="a41f9c2deadbeef", msg="Adapt STEP to GLB converter\n\nlonger body",
               date="2026-09-15T14:02:11Z"):
    return 200, json.dumps({"sha": sha, "commit": {"message": msg, "committer": {"date": date}}})


def test_protected_because_it_is_an_egress_boundary():
    assert constitution.is_protected("helix/adapters/github_fleet.py")


def test_reads_head_short_sha_subject_and_time():
    http = Http({"/commits/main": commit_doc()})
    r = gh.GithubRepos(lambda: "ghp_x", http_get=http).read_head("Alex-Mark1/WMS_V1", "main")
    assert r.ok and r.commit == "a41f9c2" and r.full_sha == "a41f9c2deadbeef"
    assert r.subject == "Adapt STEP to GLB converter"
    assert r.committed_at.year == 2026


def test_every_request_goes_to_api_github_com_and_nowhere_else():
    http = Http({"/commits/main": commit_doc()})
    gh.GithubRepos(lambda: "ghp_x", http_get=http).read_head("Alex-Mark1/WMS_V1", "main")
    (url, headers), = http.calls
    assert url.startswith("https://api.github.com/repos/Alex-Mark1/WMS_V1/commits/main")


def test_the_token_rides_as_a_bearer_header_and_is_never_in_the_url():
    http = Http({"/commits/main": commit_doc()})
    gh.GithubRepos(lambda: "ghp_secret", http_get=http).read_head("o/n", "main")
    (url, headers), = http.calls
    assert "ghp_secret" not in url
    assert headers["Authorization"] == "Bearer ghp_secret"


def test_a_slash_in_the_branch_name_is_escaped_not_a_path():
    http = Http({"/commits/": commit_doc()})
    gh.GithubRepos(lambda: "t", http_get=http).read_head("o/n", "feature/x")
    (url, _), = http.calls
    assert url.endswith("/commits/feature%2Fx")


def test_no_token_is_its_own_sentence_and_no_request_is_made():
    http = Http({})
    r = gh.GithubRepos(lambda: "", http_get=http).read_head("o/n", "main")
    assert not r.ok and r.problem == gh.NO_TOKEN and http.calls == []
    assert gh.GithubRepos(lambda: None, http_get=http).available() == (False, gh.NO_TOKEN)


@pytest.mark.parametrize("status,expect", [
    (404, gh.NOT_FOUND), (403, gh.RATE_LIMITED), (429, gh.RATE_LIMITED),
    (0, gh.UNREACHABLE), (500, gh.BAD_OUTPUT),
])
def test_failure_kinds_are_named(status, expect):
    http = Http({"/commits/main": (status, "")})
    r = gh.GithubRepos(lambda: "t", http_get=http).read_head("o/n", "main")
    assert not r.ok and r.problem == expect


def test_a_repo_not_shaped_owner_slash_name_is_refused_before_any_request():
    http = Http({})
    r = gh.GithubRepos(lambda: "t", http_get=http).read_head("just-a-name", "main")
    assert not r.ok and http.calls == []


# ---------------------------------------------------------------------------- compare, flipped once

def compare_doc(status, ahead_by, behind_by):
    return 200, json.dumps({"status": status, "ahead_by": ahead_by, "behind_by": behind_by})


@pytest.mark.parametrize("github_says,ahead,behind,cell_sees,behind_by", [
    ("identical", 0, 0, "identical", 0),
    ("ahead", 11, 0, "behind", 11),     # repo moved on -> serving is BEHIND by 11 (normal)
    ("behind", 0, 2, "ahead", 0),       # serving has commits the branch lacks -> AHEAD
    ("diverged", 3, 1, "diverged", 3),
])
def test_compare_reports_from_the_serving_commits_point_of_view(github_says, ahead, behind, cell_sees, behind_by):
    http = Http({"/compare/": compare_doc(github_says, ahead, behind)})
    r = gh.GithubRepos(lambda: "t", http_get=http).compare("o/n", "6d1ac83", "a41f9c2")
    assert r.ok and r.status == cell_sees and r.behind_by == behind_by
    (url, _), = http.calls
    assert url.endswith("/compare/6d1ac83...a41f9c2"), "serving is BASE, head is HEAD"


def test_compare_with_a_missing_side_is_refused_without_a_request():
    http = Http({})
    assert not gh.GithubRepos(lambda: "t", http_get=http).compare("o/n", "", "abc").ok
    assert http.calls == []


def test_problem_sentences_are_ascii():
    for s in (gh.NO_TOKEN, gh.NOT_FOUND, gh.RATE_LIMITED, gh.UNREACHABLE, gh.BAD_OUTPUT):
        fleet.assert_ascii(s, "github_fleet problem text")
