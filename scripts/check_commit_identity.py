#!/usr/bin/env python3
"""Validate commit authors and co-author trailers for a GitHub pull request."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "Mojtaba Arvin"
EMAIL = "m.arvin@live.com"
MARKER = "<!-- maintainer-directed-ai-work -->"
AUTO_LABEL = "allow-automation-commits"
AUTO_LOGINS = {"github-actions[bot]", "dependabot[bot]"}
BOT_MARKERS = ("[bot]", "ai agent", "ai-agent", "agent bot", "copilot", "claude",
               "cursor", "windsurf", "codex", "chatgpt", "gemini", "openai", "gpt", "ai assistant", "assistant bot")


def is_bot(name, email, login=None, account_type=None):
    if (account_type or "").lower() == "bot":
        return True
    if login and login.lower().endswith("[bot]"):
        return True
    value = (name + " " + email + " " + (login or "")).lower()
    return any(marker in value for marker in BOT_MARKERS)


def validate(pr, commits):
    errors = []
    labels = {x.get("name", "") for x in pr.get("labels", [])}
    allow_auto = AUTO_LABEL in labels
    maintainer_work = MARKER in (pr.get("body") or "")
    for item in commits:
        c = item.get("commit", {})
        a = c.get("author") or {}
        gh = item.get("author") or {}
        name, email = a.get("name") or "", a.get("email") or ""
        login, typ = gh.get("login"), gh.get("type")
        sha = (item.get("sha") or "unknown")[:12]
        if not name or not email:
            errors.append(sha + ": missing author name/email")
        if is_bot(name, email, login, typ):
            allowed = allow_auto and login in AUTO_LOGINS and typ == "Bot"
            if not allowed:
                errors.append(f"{sha}: bot/agent author rejected: {name!r} <{email}> login={login!r}")
        if maintainer_work and (name != NAME or email.lower() != EMAIL.lower()):
            errors.append(f"{sha}: expected {NAME} <{EMAIL}>, got {name!r} <{email}>")
        for line in (c.get("message") or "").splitlines():
            if line.lower().startswith("co-authored-by:"):
                identity = line.split(":", 1)[1].strip()
                if is_bot(identity, ""):
                    errors.append(f"{sha}: bot/AI Co-authored-by trailer rejected: {identity!r}")
    return errors


def get_json(url, token):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "commit-identity-policy-check"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def self_test():
    def item(name, email, login, typ, message="feat: test"):
        return {"sha": "a" * 40, "commit": {"author": {"name": name, "email": email},
                "message": message}, "author": {"login": login, "type": typ}}
    assert not validate({"body": "", "labels": []}, [item(NAME, EMAIL, "mojtaba-arvin", "User")])
    assert not validate({"body": "", "labels": []}, [item("External Human", "x@example.org", "human", "User")])
    assert validate({"body": "", "labels": []}, [item("Copilot", "bot@example.org", "copilot[bot]", "Bot")])
    msg = "feat: test\nCo-authored-by: Copilot <copilot[bot]@users.noreply.github.com>"
    assert validate({"body": "", "labels": []}, [item("Human", "h@e.org", "human", "User", msg)])
    assert not validate({"body": MARKER, "labels": []}, [item(NAME, EMAIL, "mojtaba-arvin", "User")])
    assert validate({"body": MARKER, "labels": []}, [item("Other Human", "x@example.org", "human", "User")])
    assert not validate({"body": "", "labels": [{"name": AUTO_LABEL}]},
                        [item("github-actions[bot]", "bot@users.noreply.github.com", "github-actions[bot]", "Bot")])
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
    if not (args.repository and args.pr_number and args.token):
        if args.self_test and not args.repository and not args.pr_number and not args.token:
            return 0
        p.error("--repository, --pr-number and --token are required")
    base = "https://api.github.com/repos/" + args.repository
    pr = get_json(base + "/pulls/" + str(args.pr_number), args.token)
    commits, page = [], 1
    while True:
        batch = get_json(base + f"/pulls/{args.pr_number}/commits?per_page=100&page={page}", args.token)
        if not isinstance(batch, list):
            raise RuntimeError("GitHub API returned a non-list commit response")
        commits.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not commits:
        print("ERROR: empty PR commit list; refusing to pass")
        return 2
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
    except urllib.error.HTTPError as e:
        print(f"ERROR: GitHub API HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(2)
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as e:
        print(f"ERROR: identity validation incomplete: {e}", file=sys.stderr)
        sys.exit(2)
