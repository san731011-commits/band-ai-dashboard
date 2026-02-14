#!/usr/bin/env python3
"""Discord sidecar bot that routes slash commands to Codex HTTP bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands


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


def load_config(repo_path: pathlib.Path, env_file: str) -> dict[str, str]:
    config = dict(os.environ)
    config_from_file = parse_env_file((repo_path / env_file).resolve())
    for k, v in config_from_file.items():
        config.setdefault(k, v)
    return config


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout_sec: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_sec = timeout_sec

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "discord-codex-bridge",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {err.code}: {detail}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Bridge request failed: {err}") from err

    def create_job(self, prompt: str) -> dict[str, Any]:
        return self._request("POST", "/jobs", {"prompt": prompt})

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")


def truncate(text: str, limit: int = 1400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def parse_channel_allowlist(raw: str) -> set[int]:
    if not raw.strip():
        return set()
    out: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            out.add(int(item))
    return out


@dataclass
class BotSettings:
    poll_interval_sec: float
    max_wait_sec: int
    allowed_channels: set[int]


class CodexDiscordBot(discord.Client):
    def __init__(self, bridge: BridgeClient, settings: BotSettings, guild_id: int | None):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.bridge = bridge
        self.settings = settings
        self.guild_id = guild_id
        self._register_commands()

    async def setup_hook(self) -> None:
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def _channel_allowed(self, channel_id: int) -> bool:
        if not self.settings.allowed_channels:
            return True
        return channel_id in self.settings.allowed_channels

    def _guard_channel(self, interaction: discord.Interaction) -> bool:
        channel_id = interaction.channel_id or 0
        return self._channel_allowed(channel_id)

    def _register_commands(self) -> None:
        @self.tree.command(name="task", description="Codex 작업을 비동기로 접수합니다.")
        @app_commands.describe(prompt="Codex에 전달할 작업 지시")
        async def task_cmd(interaction: discord.Interaction, prompt: str) -> None:
            if not self._guard_channel(interaction):
                await interaction.response.send_message("이 채널에서는 명령 실행이 제한되어 있어.", ephemeral=True)
                return

            await interaction.response.defer(thinking=True)
            try:
                created = await asyncio.to_thread(self.bridge.create_job, prompt)
                job_id = created.get("job_id", "")
                await interaction.followup.send(
                    f"접수 완료\n- job_id: `{job_id}`\n- 상태 확인: `/status job_id:{job_id}`"
                )
                # Don't rely on interaction followups for long-running background notifications.
                # Interaction tokens can expire; channel messages are more reliable.
                asyncio.create_task(self._watch_job_and_report(interaction, job_id))
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"작업 접수 실패: `{exc}`")

        @self.tree.command(name="status", description="job_id 상태를 조회합니다.")
        @app_commands.describe(job_id="조회할 job id")
        async def status_cmd(interaction: discord.Interaction, job_id: str) -> None:
            if not self._guard_channel(interaction):
                await interaction.response.send_message("이 채널에서는 명령 실행이 제한되어 있어.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            try:
                job = await asyncio.to_thread(self.bridge.get_job, job_id)
                await interaction.followup.send(self._format_status(job))
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"상태 조회 실패: `{exc}`")

        @self.tree.command(name="health", description="브리지 서버 상태를 확인합니다.")
        async def health_cmd(interaction: discord.Interaction) -> None:
            if not self._guard_channel(interaction):
                await interaction.response.send_message("이 채널에서는 명령 실행이 제한되어 있어.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            try:
                health = await asyncio.to_thread(self.bridge.health)
                msg = (
                    "브리지 상태\n"
                    f"- ok: `{health.get('ok')}`\n"
                    f"- queued: `{health.get('queued')}`\n"
                    f"- running: `{health.get('running')}`\n"
                    f"- time: `{health.get('time')}`"
                )
                await interaction.followup.send(msg)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"헬스 체크 실패: `{exc}`")

    def _format_status(self, job: dict[str, Any]) -> str:
        status = job.get("status", "unknown")
        lines = [
            f"- job_id: `{job.get('id', '-')}`",
            f"- status: `{status}`",
            f"- return_code: `{job.get('return_code')}`",
        ]
        if status in {"queued", "running"}:
            return "작업 진행 중\n" + "\n".join(lines)

        stdout = truncate(job.get("stdout", ""))
        stderr = truncate(job.get("stderr", ""))
        error = truncate(job.get("error", ""))

        if stdout:
            lines.append(f"- stdout:\n```text\n{stdout}\n```")
        if stderr:
            lines.append(f"- stderr:\n```text\n{stderr}\n```")
        if error:
            lines.append(f"- error: `{error}`")
        return "작업 완료\n" + "\n".join(lines)

    async def _watch_job_and_report(self, interaction: discord.Interaction, job_id: str) -> None:
        """Poll the bridge and report completion back to the channel.

        We intentionally prefer channel messages over interaction followups because
        interaction tokens can expire or become invalid after reconnects.
        """

        async def send_to_channel(message: str) -> None:
            try:
                channel = interaction.channel
                if channel is None and interaction.channel_id:
                    channel = await self.fetch_channel(interaction.channel_id)
                if channel is not None and hasattr(channel, "send"):
                    await channel.send(message)
                    return
            except Exception:  # noqa: BLE001
                pass
            # Best-effort fallback (may fail if interaction token expired).
            try:
                await interaction.followup.send(message)
            except Exception:  # noqa: BLE001
                print(f"[watch] failed to send message for job {job_id}", file=sys.stderr)

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.settings.max_wait_sec
            last_status = "queued"
            while loop.time() < deadline:
                try:
                    job = await asyncio.to_thread(self.bridge.get_job, job_id)
                except Exception as exc:  # noqa: BLE001
                    await send_to_channel(f"job `{job_id}` 조회 오류: `{exc}`")
                    return

                status = str(job.get("status", "unknown"))
                if status in {"succeeded", "failed"}:
                    await send_to_channel(
                        f"<@{interaction.user.id}> job `{job_id}` 완료\n{self._format_status(job)}"
                    )
                    return
                last_status = status
                await asyncio.sleep(self.settings.poll_interval_sec)

            await send_to_channel(
                f"job `{job_id}` 장기 실행 중(`{last_status}`). `/status job_id:{job_id}`로 계속 조회해줘."
            )
        except Exception as exc:  # noqa: BLE001
            # Never let background task exceptions disappear silently.
            print(f"[watch] unexpected error job={job_id}: {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord -> Codex HTTP bridge bot")
    parser.add_argument("--repo-path", default=".", help="Repository path for env lookup")
    parser.add_argument("--env-file", default=".worker.env", help="Environment file name")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_path = pathlib.Path(args.repo_path).resolve()
    config = load_config(repo_path, args.env_file)

    discord_token = config.get("DISCORD_BOT_TOKEN", "").strip()
    bridge_token = config.get("BRIDGE_TOKEN", "").strip()
    bridge_base_url = config.get("BRIDGE_BASE_URL", "http://127.0.0.1:8787").strip()
    if not discord_token:
        print("DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
        return 1
    if not bridge_token:
        print("BRIDGE_TOKEN is not set.", file=sys.stderr)
        return 1

    bridge_timeout = int(config.get("BRIDGE_HTTP_TIMEOUT_SEC", "20"))
    poll_interval_sec = float(config.get("DISCORD_BRIDGE_POLL_INTERVAL_SEC", "2"))
    max_wait_sec = int(config.get("DISCORD_BRIDGE_MAX_WAIT_SEC", "900"))
    guild_id = int(config["DISCORD_GUILD_ID"]) if config.get("DISCORD_GUILD_ID", "").isdigit() else None
    allowed_channels = parse_channel_allowlist(config.get("DISCORD_ALLOWED_CHANNELS", ""))

    bridge = BridgeClient(base_url=bridge_base_url, token=bridge_token, timeout_sec=bridge_timeout)
    settings = BotSettings(
        poll_interval_sec=max(poll_interval_sec, 1.0),
        max_wait_sec=max(max_wait_sec, 30),
        allowed_channels=allowed_channels,
    )

    bot = CodexDiscordBot(bridge=bridge, settings=settings, guild_id=guild_id)
    bot.run(discord_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
