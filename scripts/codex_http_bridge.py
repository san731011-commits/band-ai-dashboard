#!/usr/bin/env python3
"""Async HTTP bridge for running Codex jobs with token auth."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import queue
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    prompt: str
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class JobStore:
    def __init__(self, max_output_chars: int):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self.max_output_chars = max_output_chars

    def create(self, prompt: str) -> Job:
        job = Job(id=str(uuid.uuid4()), prompt=prompt)
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self.lock:
            job = self.jobs[job_id]
            for k, v in kwargs.items():
                setattr(job, k, v)

    def to_dict(self, job: Job) -> dict[str, Any]:
        return {
            "id": job.id,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "return_code": job.return_code,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "error": job.error,
        }

    def metrics(self) -> dict[str, int]:
        with self.lock:
            running = sum(1 for x in self.jobs.values() if x.status == "running")
            queued = sum(1 for x in self.jobs.values() if x.status == "queued")
        return {"queued": queued, "running": running}


class CodexRunner:
    def __init__(self, repo_path: pathlib.Path, command: str, prompt_mode: str, timeout_sec: int):
        self.repo_path = repo_path
        self.command = command
        self.prompt_mode = prompt_mode
        self.timeout_sec = timeout_sec

    def run(self, prompt: str) -> tuple[int, str, str]:
        cmd = shlex.split(self.command)
        if not cmd:
            raise RuntimeError("CODEX_COMMAND is empty")

        if self.prompt_mode == "arg":
            cmd = [*cmd, prompt]
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                text=True,
                capture_output=True,
                timeout=self.timeout_sec,
            )
        elif self.prompt_mode == "stdin":
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                text=True,
                input=prompt,
                capture_output=True,
                timeout=self.timeout_sec,
            )
        else:
            raise RuntimeError("CODEX_PROMPT_MODE must be 'arg' or 'stdin'")

        out = proc.stdout or ""
        err = proc.stderr or ""
        if proc.returncode != 0 and not out.strip() and not err.strip():
            err = (
                "codex exited with non-zero status and no output. "
                f"command={shlex.join(cmd)} cwd={self.repo_path}"
            )

        return proc.returncode, out, err


def load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


def start_worker(job_store: JobStore, runner: CodexRunner) -> threading.Thread:
    def loop() -> None:
        while True:
            job_id = job_store.queue.get()
            job = job_store.get(job_id)
            if job is None:
                continue

            job_store.update(job_id, status="running", started_at=now_iso())
            try:
                code, out, err = runner.run(job.prompt)
                job_store.update(
                    job_id,
                    status="succeeded" if code == 0 else "failed",
                    return_code=code,
                    stdout=trim_text(out, job_store.max_output_chars),
                    stderr=trim_text(err, job_store.max_output_chars),
                    finished_at=now_iso(),
                )
            except Exception as exc:  # noqa: BLE001
                job_store.update(
                    job_id,
                    status="failed",
                    error=str(exc),
                    finished_at=now_iso(),
                )
            finally:
                job_store.queue.task_done()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def auth_ok(handler: BaseHTTPRequestHandler, token: str) -> bool:
    given = handler.headers.get("Authorization", "")
    if given.startswith("Bearer "):
        return given[7:].strip() == token
    given2 = handler.headers.get("X-Bridge-Token", "")
    return bool(given2) and given2 == token


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json body") from exc


def make_handler(job_store: JobStore, token: str, max_prompt_chars: int):  # noqa: ANN001
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexBridge/1.0"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            # keep default noise low
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._send(HTTPStatus.OK, {"ok": True, "time": now_iso(), **job_store.metrics()})
                return

            if not auth_ok(self, token):
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            if path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/").strip()
                job = job_store.get(job_id)
                if job is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return
                self._send(HTTPStatus.OK, job_store.to_dict(job))
                return

            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/jobs":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            if not auth_ok(self, token):
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            try:
                payload = read_json(self)
                prompt = str(payload.get("prompt", "")).strip()
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if not prompt:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "prompt is required"})
                return
            if len(prompt) > max_prompt_chars:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"prompt too long (max {max_prompt_chars} chars)"},
                )
                return

            job = job_store.create(prompt)
            self._send(
                HTTPStatus.ACCEPTED,
                {
                    "job_id": job.id,
                    "status": job.status,
                    "status_url": f"/jobs/{job.id}",
                    "created_at": job.created_at,
                },
            )

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex HTTP bridge")
    parser.add_argument("--host", default=os.environ.get("BRIDGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_PORT", "8787")))
    parser.add_argument("--repo-path", default=os.environ.get("BRIDGE_REPO_PATH", "."))
    parser.add_argument("--env-file", default=".worker.env")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_path = pathlib.Path(args.repo_path).resolve()
    load_env_file((repo_path / args.env_file).resolve())

    token = os.environ.get("BRIDGE_TOKEN", "").strip()
    if not token:
        raise SystemExit("BRIDGE_TOKEN is not set.")

    max_prompt_chars = int(os.environ.get("BRIDGE_MAX_PROMPT_CHARS", "12000"))
    max_output_chars = int(os.environ.get("BRIDGE_MAX_OUTPUT_CHARS", "30000"))

    codex_command = os.environ.get("CODEX_COMMAND", "codex exec")
    codex_prompt_mode = os.environ.get("CODEX_PROMPT_MODE", "arg")
    codex_timeout_sec = int(os.environ.get("CODEX_TIMEOUT_SEC", "1800"))

    runner = CodexRunner(
        repo_path=repo_path,
        command=codex_command,
        prompt_mode=codex_prompt_mode,
        timeout_sec=codex_timeout_sec,
    )
    store = JobStore(max_output_chars=max_output_chars)
    start_worker(store, runner)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token, max_prompt_chars))
    print(f"[{now_iso()}] Codex HTTP bridge running on http://{args.host}:{args.port}")
    print(f"[{now_iso()}] repo_path={repo_path}")
    print(f"[{now_iso()}] codex_command={codex_command}")
    print(f"[{now_iso()}] prompt_mode={codex_prompt_mode}, timeout={codex_timeout_sec}s")
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
