#!/usr/bin/env python3
"""Validate commit authors and co-author trailers for a GitHub pull request."""
import argparse
import http.client
import json
import os
import re
import sys
import urllib.parse
from email.utils import parseaddr

NAME = "Mojtaba Arvin"
EMAIL = "m.arvin@live.com"
MARKER = "<!-- maintainer-directed-ai-work -->"
AUTO_LABEL = "allow-automation-commits"
AUTO_LOGINS = {"github-actions[bot]", "dependabot[bot]"}
API_ORIGIN = ("https", "api.github.com", 443)
MAX_PR_COMMITS = 250
# Exact generic/product identities only. Never substring-match a human's name.
BOT_NAMES = {
    "ai", "ai assistant", "assistant", "assistant bot", "ai agent",
    "ai-agent", "agent bot", "copilot", "claude", "cursor", "windsurf",
    "codex", "chatgpt", "gemini", "openai", "gpt",
}
BOT_EMAILS = {"assistant@example.org"}


def is_bot(name, email, login=None, account_type=None):
    if (account_type or "").lower() == "bot":
        return True
    if login and login.lower().endswith("[bot]"):
        return True
    normalized_name = " ".join((name or "").casefold().split()).strip(" ._-")
    normalized_email = (email or "").strip().casefold()
    return normalized_name in BOT_NAMES or normalized_email in BOT_EMAILS


def validate(pr, commits):
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub API returned a non-object PR response")
    if not isinstance(pr.get("labels", []), list):
        raise RuntimeError("GitHub API returned invalid PR labels")
    errors = []
    labels = {x.get("name", "") for x in pr.get("labels", []) if isinstance(x, dict)}
    allow_auto = AUTO_LABEL in labels
    maintainer_work = MARKER in (pr.get("body") or "")
    maintainer_commit_seen = False
    for item in commits:
        if not isinstance(item, dict):
            raise RuntimeError("GitHub API returned an invalid commit entry")
        c = item.get("commit", {})
        if not isinstance(c, dict):
            raise RuntimeError("GitHub API returned an invalid commit object")
        a = c.get("author") or {}
        gh = item.get("author") or {}
        if not isinstance(a, dict) or not isinstance(gh, dict):
            raise RuntimeError("GitHub API returned invalid commit author metadata")
        name, email = a.get("name") or "", a.get("email") or ""
        login, typ = gh.get("login"), gh.get("type")
        sha = (item.get("sha") or "unknown")[:12]
        if not name or not email:
            errors.append(sha + ": missing author name/email")
        if is_bot(name, email, login, typ):
            allowed = allow_auto and login in AUTO_LOGINS and typ == "Bot"
            if not allowed:
                errors.append(f"{sha}: bot/agent author rejected: {name!r} <{email}> login={login!r}")
        if login == "mojtaba-arvin":
            maintainer_commit_seen = True
            if name != NAME or email.lower() != EMAIL.lower():
                errors.append(
                    f"{sha}: maintainer account must use {NAME} <{EMAIL}>, "
                    f"got {name!r} <{email}>"
                )
        elif email.lower() == EMAIL.lower():
            errors.append(
                f"{sha}: maintainer email must resolve to mojtaba-arvin, "
                f"not {login!r}"
            )
        for line in (c.get("message") or "").splitlines():
            if line.lower().startswith("co-authored-by:"):
                co_name, co_email = parseaddr(line.split(":", 1)[1].strip())
                if is_bot(co_name, co_email):
                    errors.append(
                        f"{sha}: bot/AI Co-authored-by trailer rejected: "
                        f"{co_name!r} <{co_email}>"
                    )
    if maintainer_work and not maintainer_commit_seen:
        errors.append(
            "maintainer-directed AI work marker requires at least one commit "
            "authored by mojtaba-arvin"
        )
    return errors


def validate_api_url(url, repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("repository must be an owner/name pair")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != API_ORIGIN[0]
        or parsed.hostname != API_ORIGIN[1]
        or parsed.port not in (None, API_ORIGIN[2])
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/repos/" + repository + "/")
    ):
        raise RuntimeError("refusing GitHub API URL outside the intended HTTPS repository origin")
    return parsed


def get_json(url, token, repository):
    parsed = validate_api_url(url, repository)
    # Use a fixed HTTPS connection directly: redirects are not followed and the
    # caller cannot select a host, scheme, port, or repository path.
    connection = http.client.HTTPSConnection(API_ORIGIN[1], timeout=30)
    try:
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        connection.request("GET", path, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "commit-identity-policy-check"})
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"GitHub API HTTP {response.status}: {response.reason}")
        payload = response.read(2_097_153)
        if len(payload) > 2_097_152:
            raise RuntimeError("GitHub API response exceeded the 2 MiB safety limit")
        return json.loads(payload.decode())
    finally:
        connection.close()


def collect_commits(pr, fetch_page):
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub API returned a non-object PR response")
    expected = pr.get("commits")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise RuntimeError("GitHub API returned an invalid PR commit count")
    if expected > MAX_PR_COMMITS:
        raise RuntimeError(
            f"PR reports {expected} commits; GitHub's pull-request commits endpoint "
            f"is capped at {MAX_PR_COMMITS}; refusing incomplete validation"
        )
    commits, page = [], 1
    while True:
        batch = fetch_page(page)
        if not isinstance(batch, list):
            raise RuntimeError("GitHub API returned a non-list commit response")
        if any(not isinstance(item, dict) or not item.get("sha") for item in batch):
            raise RuntimeError("GitHub API returned an invalid commit entry")
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    shas = [item["sha"] for item in commits]
    if len(shas) != len(set(shas)):
        raise RuntimeError("GitHub API returned duplicate commits; refusing incomplete validation")
    if len(commits) != expected:
        raise RuntimeError(
            f"incomplete commit coverage: PR reports {expected}, API returned {len(commits)}"
        )
    return commits


def self_test():
    def item(name, email, login, typ, message="feat: test", sha=None):
        return {"sha": sha or "a" * 40, "commit": {
            "author": {"name": name, "email": email}, "message": message},
            "author": {"login": login, "type": typ}}

    empty_pr = {"body": "", "labels": []}
    assert not validate(empty_pr, [item(NAME, EMAIL, "mojtaba-arvin", "User")])
    assert not validate(empty_pr, [item("External Human", "x@example.org", "human", "User")])
    for human_name in ("Claude Dupont", "OpenAI Smith", "GPT Anderson",
                       "Assistant Jones", "Copilot Lee", "Gemini Patel"):
        assert not validate(empty_pr, [item(human_name, "human@example.org", "human", "User")])
    assert validate(empty_pr, [item("Copilot", "bot@example.org", "copilot[bot]", "Bot")])
    assert validate(empty_pr, [item("AI Assistant", "ai@example.org", None, None)])
    assert validate(empty_pr, [item("Generic", "assistant@example.org", None, None)])
    for identity in ("AI Assistant <ai@example.org>", "Assistant <assistant@example.org>"):
        msg = "feat: test\nCo-authored-by: " + identity
        assert validate(empty_pr, [item("Human", "h@example.org", "human", "User", msg)])
    human_trailer = "feat: test\nCo-authored-by: Claude Dupont <claude@example.org>"
    assert not validate(empty_pr, [item("Human", "h@example.org", "human", "User", human_trailer)])
    assert not validate({"body": MARKER, "labels": []},
                        [item(NAME, EMAIL, "mojtaba-arvin", "User")])
    assert not validate({"body": MARKER, "labels": []}, [
        item(NAME, EMAIL, "mojtaba-arvin", "User"),
        item("Other Human", "x@example.org", "human", "User")])
    assert validate({"body": MARKER, "labels": []},
                    [item("Other Human", "x@example.org", "human", "User")])
    assert validate(empty_pr, [item(NAME, "wrong@example.org", "mojtaba-arvin", "User")])
    assert not validate({"body": "", "labels": [{"name": AUTO_LABEL}]}, [
        item("github-actions[bot]", "bot@users.noreply.github.com",
             "github-actions[bot]", "Bot")])

    def page_fixture(count):
        return [{"sha": f"{i:040x}", "commit": {}} for i in range(count)]
    complete = page_fixture(250)
    assert len(collect_commits({"commits": 250},
                               lambda page: complete[(page - 1) * 100:page * 100])) == 250
    try:
        collect_commits({"commits": 251},
                        lambda page: complete[(page - 1) * 100:page * 100])
        raise AssertionError("251-commit PR must fail closed")
    except RuntimeError as exc:
        assert "capped" in str(exc)
    try:
        collect_commits({"commits": 101}, lambda page: page_fixture(100) if page == 1 else [])
        raise AssertionError("incomplete page coverage must fail closed")
    except RuntimeError as exc:
        assert "incomplete commit coverage" in str(exc)
    try:
        collect_commits({"commits": 1}, lambda page: {"not": "a list"})
        raise AssertionError("non-list commit response must fail closed")
    except RuntimeError as exc:
        assert "non-list commit response" in str(exc)
    duplicate = page_fixture(1) * 2
    try:
        collect_commits({"commits": 2}, lambda page: duplicate if page == 1 else [])
        raise AssertionError("duplicate commit response must fail closed")
    except RuntimeError as exc:
        assert "duplicate commits" in str(exc)
    try:
        collect_commits([], lambda page: [])
        raise AssertionError("non-object PR response must fail closed")
    except RuntimeError as exc:
        assert "non-object PR" in str(exc)
    for bad_url in (
        "http://api.github.com/repos/owner/repo/pulls/1",
        "https://evil.example/repos/owner/repo/pulls/1",
        "https://api.github.com.evil.example/repos/owner/repo/pulls/1",
        "https://api.github.com/repos/other/repo/pulls/1",
    ):
        try:
            validate_api_url(bad_url, "owner/repo")
            raise AssertionError("unsafe API URL must be rejected")
        except RuntimeError:
            pass
    validate_api_url("https://api.github.com/repos/owner/repo/pulls/1", "owner/repo")
    print("PASS: commit identity policy self-tests")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY"))
    p.add_argument("--pr-number", type=int, default=int(os.getenv("PR_NUMBER", "0")))
    p.add_argument("--token", default=os.getenv("GITHUB_TOKEN"))
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not (args.repository and args.pr_number and args.token):
        p.error("--repository, --pr-number and --token are required")
    if args.pr_number < 1:
        p.error("--pr-number must be positive")
    base = "https://api.github.com/repos/" + args.repository
    pr = get_json(base + "/pulls/" + str(args.pr_number), args.token, args.repository)
    if not isinstance(pr, dict):
        raise RuntimeError("GitHub API returned a non-object PR response")
    commits = collect_commits(
        pr, lambda page: get_json(
            base + f"/pulls/{args.pr_number}/commits?per_page=100&page={page}",
            args.token, args.repository))
    errors = validate(pr, commits)
    print(f"Checked {len(commits)} commits in {args.repository}#{args.pr_number}")
    if errors:
        print("\n".join("ERROR: " + e for e in errors))
        return 1
    print("PASS: no prohibited bot/agent authors or co-author trailers")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (http.client.HTTPException, TimeoutError, ValueError, RuntimeError) as e:
        print(f"ERROR: identity validation incomplete: {e}", file=sys.stderr)
        sys.exit(2)
