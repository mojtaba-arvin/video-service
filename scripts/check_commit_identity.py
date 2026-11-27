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
