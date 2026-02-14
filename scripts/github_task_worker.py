#!/usr/bin/env python3
"""GitHub issue worker for OpenClaw -> Codex automation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_OWNER = "san731011-commits"
DEFAULT_REPO = "band-ai-dashboard"


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


class GitHubClient:
    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.base = f"https://api.github.com/repos/{owner}/{repo}"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "band-ai-dashboard-worker",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed ({e.code}): {detail}") from e

    def ensure_label(self, name: str, color: str, description: str) -> None:
        path = f"/labels/{urllib.parse.quote(name, safe='')}"
        try:
            self._request("GET", path)
            return
        except RuntimeError as err:
            if "(404)" not in str(err):
                raise
        self._request("POST", "/labels", {"name": name, "color": color, "description": description})

    def list_open_task_issues(self) -> list[dict[str, Any]]:
        query = "?state=open&labels=task&sort=created&direction=asc&per_page=20"
        items = self._request("GET", f"/issues{query}")
        return [x for x in items if "pull_request" not in x]

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/issues/{number}")

    def replace_labels(self, issue_number: int, labels: list[str]) -> None:
        self._request("PATCH", f"/issues/{issue_number}", {"labels": labels})

    def comment_issue(self, issue_number: int, body: str) -> None:
        self._request("POST", f"/issues/{issue_number}/comments", {"body": body})

    def find_open_pr_by_branch(self, branch: str) -> dict[str, Any] | None:
        head = urllib.parse.quote(f"{self.owner}:{branch}", safe="")
        pulls = self._request("GET", f"/pulls?state=open&head={head}")
        return pulls[0] if pulls else None

    def create_pr(self, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self._request("POST", "/pulls", {"title": title, "head": head, "base": base, "body": body})


def run(cmd: list[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:40] if slug else "task"


def build_prompt(issue: dict[str, Any], repo_path: pathlib.Path) -> str:
    return (
        "You are working in this local repository.\n"
        f"- Repo path: {repo_path}\n"
        f"- GitHub issue: #{issue['number']} {issue['title']}\n\n"
        "Issue body:\n"
        f"{issue.get('body') or '(empty)'}\n\n"
        "Task:\n"
        "1) Implement the requested change in code.\n"
        "2) Run relevant validation/tests.\n"
        "3) Keep changes scoped to this issue.\n"
        "4) Summarize what changed and any risks.\n"
    )


def ensure_git_identity(repo_path: pathlib.Path) -> None:
    name = os.environ.get("GIT_AUTHOR_NAME", "san731011-commits (san731011-commits)")
    email = os.environ.get("GIT_AUTHOR_EMAIL", "san731011@gmail.com")
    run(["git", "config", "user.name", name], cwd=repo_path)
    run(["git", "config", "user.email", email], cwd=repo_path)


def checkout_branch(repo_path: pathlib.Path, branch: str) -> None:
    run(["git", "fetch", "origin"], cwd=repo_path)
    run(["git", "checkout", "main"], cwd=repo_path)
    run(["git", "pull", "--ff-only", "origin", "main"], cwd=repo_path)

    probe = run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo_path, check=False)
    if probe.returncode == 0:
        run(["git", "checkout", branch], cwd=repo_path)
    else:
        run(["git", "checkout", "-b", branch, "main"], cwd=repo_path)


def has_local_changes(repo_path: pathlib.Path) -> bool:
    proc = run(["git", "status", "--porcelain"], cwd=repo_path)
    return bool(proc.stdout.strip())


def commit_and_push(repo_path: pathlib.Path, branch: str, issue_number: int) -> bool:
    if not has_local_changes(repo_path):
        return False
    run(["git", "add", "-A"], cwd=repo_path)
    run(["git", "commit", "-m", f"feat: implement issue #{issue_number}"], cwd=repo_path)
    run(["git", "push", "-u", "origin", branch], cwd=repo_path)
    return True


def execute_codex(prompt: str, repo_path: pathlib.Path, issue_number: int, run_dir: pathlib.Path) -> tuple[int, pathlib.Path]:
    codex_cmd = os.environ.get("CODEX_COMMAND", "codex exec").strip()
    prompt_mode = os.environ.get("CODEX_PROMPT_MODE", "arg").strip().lower()
    timeout_sec = int(os.environ.get("CODEX_TIMEOUT_SEC", "1800"))

    cmd = shlex.split(codex_cmd)
    if not cmd:
        raise RuntimeError("CODEX_COMMAND is empty.")

    if prompt_mode not in {"arg", "stdin"}:
        raise RuntimeError("CODEX_PROMPT_MODE must be 'arg' or 'stdin'.")

    if prompt_mode == "arg":
        cmd = [*cmd, prompt]
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    else:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            text=True,
            input=prompt,
            capture_output=True,
            timeout=timeout_sec,
        )

    log_path = run_dir / f"issue-{issue_number}-codex.log"
    log_path.write_text(
        f"$ {' '.join(cmd)}\n\n[stdout]\n{proc.stdout}\n\n[stderr]\n{proc.stderr}\n",
        encoding="utf-8",
    )
    return proc.returncode, log_path


def issue_labels(issue: dict[str, Any]) -> list[str]:
    return [x["name"] for x in issue.get("labels", [])]


def set_state_labels(gh: GitHubClient, issue: dict[str, Any], *, add: list[str], remove: list[str]) -> None:
    labels = set(issue_labels(issue))
    for name in remove:
        labels.discard(name)
    for name in add:
        labels.add(name)
    gh.replace_labels(issue["number"], sorted(labels))


def pick_next_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    for issue in issues:
        labels = set(issue_labels(issue))
        if "in-progress" in labels or "done" in labels or "canceled" in labels:
            continue
        return issue
    return None


def process_issue(gh: GitHubClient, issue: dict[str, Any], repo_path: pathlib.Path, dry_run: bool) -> None:
    issue_number = issue["number"]
    branch = f"task/{issue_number}-{slugify(issue['title'])}"
    run_dir = repo_path / "worker_runs"
    run_dir.mkdir(exist_ok=True)

    gh.comment_issue(issue_number, f"Worker picked this task at `{now_utc_iso()}`.")
    set_state_labels(gh, issue, add=["in-progress"], remove=["failed"])

    prompt = build_prompt(issue, repo_path)
    prompt_path = run_dir / f"issue-{issue_number}-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    if dry_run:
        gh.comment_issue(issue_number, f"Dry-run complete. Prepared prompt at `{prompt_path}`.")
        return

    ensure_git_identity(repo_path)
    checkout_branch(repo_path, branch)

    return_code, log_path = execute_codex(prompt, repo_path, issue_number, run_dir)
    if return_code != 0:
        fresh = gh.get_issue(issue_number)
        set_state_labels(gh, fresh, add=["failed"], remove=["in-progress"])
        gh.comment_issue(
            issue_number,
            "Worker execution failed.\n\n"
            f"- Exit code: `{return_code}`\n"
            f"- Log file: `{log_path}`\n"
            "Please review worker logs on the home PC.",
        )
        return

    changed = commit_and_push(repo_path, branch, issue_number)
    fresh = gh.get_issue(issue_number)
    if not changed:
        set_state_labels(gh, fresh, add=["done"], remove=["in-progress"])
        gh.comment_issue(
            issue_number,
            "Worker finished but no file changes were detected. "
            "Review task detail if additional instructions are needed.",
        )
        return

    pr = gh.find_open_pr_by_branch(branch)
    if pr is None:
        pr = gh.create_pr(
            title=f"[Task #{issue_number}] {issue['title']}",
            head=branch,
            base="main",
            body=(
                f"Closes #{issue_number}\n\n"
                "Generated by automated GitHub worker.\n"
                f"- Branch: `{branch}`\n"
                f"- Log file: `{log_path.name}`"
            ),
        )

    set_state_labels(gh, fresh, add=["waiting-review"], remove=["in-progress"])
    gh.comment_issue(
        issue_number,
        "Worker completed and opened/updated a PR.\n\n"
        f"- Branch: `{branch}`\n"
        f"- PR: {pr['html_url']}\n"
        f"- Log file: `{log_path}`",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub Task Worker")
    parser.add_argument("--repo-path", default=".", help="Local repository path")
    parser.add_argument("--interval", type=int, default=45, help="Polling interval in seconds")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--issue", type=int, help="Process a specific issue number")
    parser.add_argument("--dry-run", action="store_true", help="Do not run Codex or git commands")
    parser.add_argument("--env-file", default=".worker.env", help="Env file path (relative to repo path)")
    return parser.parse_args()


def load_config(repo_path: pathlib.Path, env_file: str) -> dict[str, str]:
    config = dict(os.environ)
    file_values = parse_env_file((repo_path / env_file).resolve())
    for k, v in file_values.items():
        config.setdefault(k, v)
    return config


def ensure_worker_labels(gh: GitHubClient) -> None:
    gh.ensure_label("task", "1f6feb", "Task request")
    gh.ensure_label("in-progress", "d4c5f9", "Worker is processing")
    gh.ensure_label("failed", "d73a4a", "Worker failed")
    gh.ensure_label("waiting-review", "fbca04", "PR is waiting for review")
    gh.ensure_label("done", "0e8a16", "Task finished")
    gh.ensure_label("canceled", "6e7781", "Task canceled")


def main() -> int:
    args = parse_args()
    repo_path = pathlib.Path(args.repo_path).resolve()
    config = load_config(repo_path, args.env_file)

    token = config.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is not set. Export it or place it in .worker.env", file=sys.stderr)
        return 1

    owner = config.get("GITHUB_OWNER", DEFAULT_OWNER)
    repo = config.get("GITHUB_REPO", DEFAULT_REPO)
    gh = GitHubClient(owner=owner, repo=repo, token=token)
    ensure_worker_labels(gh)

    def process_once() -> None:
        if args.issue:
            issue = gh.get_issue(args.issue)
            if "pull_request" in issue:
                print(f"#{args.issue} is a pull request. Skipping.")
                return
        else:
            issue = pick_next_issue(gh.list_open_task_issues())
            if issue is None:
                print(f"[{now_utc_iso()}] No pending task issues.")
                return
        print(f"[{now_utc_iso()}] Processing issue #{issue['number']}: {issue['title']}")
        process_issue(gh, issue, repo_path=repo_path, dry_run=args.dry_run)

    if args.loop:
        while True:
            try:
                process_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[{now_utc_iso()}] Worker error: {exc}", file=sys.stderr)
            time.sleep(max(args.interval, 5))
    else:
        process_once()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
