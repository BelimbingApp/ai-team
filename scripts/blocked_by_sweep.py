#!/usr/bin/env python3
"""Unblock issues whose declared blockers are all closed.

The module keeps parsing and label decisions free of GitHub I/O so they can be
tested without credentials or a live repository.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BLOCKED_LABEL = "task:blocked"
READY_LABEL = "task:ready"
BLOCKED_BY_RE = re.compile(
    r"(?i)(?<![\w-])Blocked-By:[ \t]*(#[0-9]+(?:[ \t]*,[ \t]*#[0-9]+)*)(?=[ \t]*(?:[.;]|$))"
)
# Only prose can declare a dependency. The guard is intentionally local: there
# is no same-repository parser to keep in lockstep with this Markdown boundary.
OPENING_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
CLOSING_FENCE_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*$")
INDENTED_CODE_RE = re.compile(r"^( {4}|[ ]*\t)")


@dataclass(frozen=True)
class Transition:
    labels: tuple[str, ...]
    comment: str | None


def safe_lines(body: str) -> list[str]:
    """Body lines that Markdown renders as prose.

    Fenced blocks, indented code and blockquotes are dropped, so documenting the
    convention inside an issue cannot arm the sweep against that issue. A fence
    closes only on a run of the same character at least as long as the one that
    opened it, so a ```` block is not ended by a ``` line.
    """

    lines: list[str] = []
    fence: str | None = None

    for line in body.split("\n"):
        raw_line = line.replace("\r", "")
        trimmed = raw_line.strip()

        if fence is not None:
            # Checked before the closer, and against the raw line: Markdown
            # reads an indented ``` as code, not as the end of the block, so a
            # parser that closes here treats the rest of the body as prose.
            if INDENTED_CODE_RE.match(raw_line):
                continue

            closing = CLOSING_FENCE_RE.match(trimmed)
            if closing and _closes_fence(closing.group(1), fence):
                fence = None
            continue

        if INDENTED_CODE_RE.match(raw_line) or trimmed.startswith(">"):
            continue

        opening = OPENING_FENCE_RE.match(trimmed)
        if opening:
            fence = opening.group(1)
            continue

        lines.append(trimmed)

    return lines


def _closes_fence(closing: str, opening: str) -> bool:
    return closing[0] == opening[0] and len(closing) >= len(opening)


def parse_blockers(body: str | None) -> tuple[int, ...]:
    """Return issue numbers from every valid prose Blocked-By declaration.

    Standalone headers and inline sentences ending the reference list with a
    period or semicolon are both declarations. All declarations are unioned
    rather than only the first: recording a new blocker by adding a line is the
    natural edit, and reading only the first silently dropped the rest -- which
    marked an issue ready while a blocker was open.

    A malformed or missing declaration contributes nothing. Duplicate
    references are collapsed while preserving their first-seen order.
    """

    numbers: list[int] = []

    for line in safe_lines(body or ""):
        for match in BLOCKED_BY_RE.finditer(line):
            numbers.extend(
                int(reference.strip()[1:]) for reference in match.group(1).split(",")
            )

    return tuple(dict.fromkeys(numbers))


def label_names(issue: dict) -> tuple[str, ...]:
    return tuple(
        label["name"] if isinstance(label, dict) else label
        for label in issue.get("labels", [])
    )


def transition_for(
    issue: dict, blocker_states: dict[int, str | None], comments: list[str] | None = None
) -> Transition | None:
    """Build an idempotent transition, or return None when it is unsafe."""

    if BLOCKED_LABEL not in label_names(issue):
        return None

    blockers = parse_blockers(issue.get("body"))
    if not blockers or any(blocker_states.get(number) != "closed" for number in blockers):
        return None

    labels = list(dict.fromkeys(label_names(issue)))
    labels = [label for label in labels if label != BLOCKED_LABEL]
    if READY_LABEL not in labels:
        labels.append(READY_LABEL)

    marker = comment_marker(blockers)
    existing_comments = comments or []
    comment = None
    if not any(marker in body for body in existing_comments):
        references = ", ".join(f"#{number}" for number in blockers)
        comment = (
            f"Blocked-By sweep: all declared blockers are closed ({references}); "
            f"marking this task ready. {marker}"
        )

    return Transition(tuple(labels), comment)


def comment_marker(blockers: tuple[int, ...]) -> str:
    references = ",".join(str(number) for number in blockers)
    return f"<!-- belimbing-blocked-by-sweep:{references} -->"


class GitHubAPI:
    def __init__(self, repository: str, token: str):
        self.base_url = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, path: str, method: str = "GET", payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "belimbing-blocked-by-sweep",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        with urlopen(request) as response:
            if response.status == 204:
                return None
            return json.load(response)

    def paginated(self, path: str, params: dict[str, str]) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            query = urlencode({**params, "per_page": "100", "page": str(page)})
            batch = self.request(f"{path}?{query}")
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1

    def open_blocked_issues(self) -> list[dict]:
        return self.paginated(
            "/issues", {"state": "open", "labels": BLOCKED_LABEL}
        )

    def issue_state(self, number: int) -> str | None:
        try:
            return self.request(f"/issues/{number}").get("state")
        except HTTPError as error:
            if error.code == 404:
                return None
            raise

    def comments(self, number: int) -> list[str]:
        return [
            comment.get("body", "")
            for comment in self.paginated(f"/issues/{number}/comments", {})
        ]

    def add_comment(self, number: int, body: str) -> None:
        self.request(f"/issues/{number}/comments", "POST", {"body": body})

    def replace_labels(self, number: int, labels: tuple[str, ...]) -> None:
        self.request(f"/issues/{number}/labels", "PUT", {"labels": list(labels)})


def sweep(api: GitHubAPI) -> int:
    transitioned = 0
    for issue in api.open_blocked_issues():
        if "pull_request" in issue:
            continue

        blockers = parse_blockers(issue.get("body"))
        if not blockers:
            continue

        states = {number: api.issue_state(number) for number in blockers}
        if any(states[number] != "closed" for number in blockers):
            continue

        transition = transition_for(issue, states, api.comments(issue["number"]))
        if transition is None:
            continue

        if transition.comment is not None:
            api.add_comment(issue["number"], transition.comment)
        api.replace_labels(issue["number"], transition.labels)
        transitioned += 1
        print(f"unblocked issue #{issue['number']}")

    return transitioned


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    sweep(GitHubAPI(repository, token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
