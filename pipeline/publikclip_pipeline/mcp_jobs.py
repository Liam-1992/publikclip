"""MCP job adapter. Video processing stays in a separate subprocess/venv."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from filelock import FileLock

from . import config
from .jobs import queue

Provider = Literal["chatgpt", "ollama", "publik", "gemini"]
Caption = Literal["classic", "beast", "hormozi", "minimal", "karaoke-pop"]
Camera = Literal["cut", "pan", "locked"]
JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"}


def job_for(job_id: str) -> queue.Job:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID; use an ID returned by list_jobs or start_job.")
    job = queue.get_job(job_id)
    if job is None:
        raise ValueError("Job not found.")
    # Never let a replaced job-directory symlink point into credential storage.
    if not job.dir.resolve().is_relative_to(config.jobs_dir().resolve()):
        raise ValueError("Job directory is outside the jobs folder.")
    return job


def read_stage(job: queue.Job, stage: str) -> dict | None:
    path = job.dir / f"{stage}.json"
    if not path.resolve().is_relative_to(job.dir.resolve()):
        raise ValueError("Checkpoint is outside the job folder.")
    if not path.exists():
        return None
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"{stage} checkpoint is too large to read.")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        data = envelope["data"]
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError) as err:
        raise ValueError(f"The {stage} checkpoint is invalid; resume the job to rebuild it.") from err


def validate_source(source: str) -> tuple[str, str]:
    source = source.strip()
    if not source or len(source) > 4096:
        raise ValueError("Provide a local video path or a YouTube URL.")
    if source.startswith(("https://", "http://")):
        url = urlsplit(source)
        if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
                or url.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}):
            raise ValueError("Remote sources must be HTTPS YouTube URLs; other sources require a local video file.")
        return "url", source
    path = Path(source).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Local source must be an existing video file on the machine running this server.")
    return "file", str(path)


@dataclass
class RunningJob:
    process: asyncio.subprocess.Process
    monitor: asyncio.Task | None = None
    stopped: bool = False
    progress: dict = field(default_factory=lambda: {
        "stage": "env", "fraction": -1, "message": "Preparing the video-processing runtime…",
    })


class JobManager:
    def __init__(self) -> None:
        self.active: dict[str, RunningJob] = {}
        self.guard = asyncio.Lock()
        self.root = config.home_dir() / "mcp"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instance_lock = FileLock(str(self.root / "server.lock"), timeout=0)

    def acquire(self) -> None:
        self.instance_lock.acquire()

    def _record(self, job_id: str, payload: dict) -> None:
        queue._atomic_write_json(self.root / f"{job_id}.json", {"updated_at": time.time(), **payload})

    def _history(self, job_id: str) -> dict:
        path = self.root / f"{job_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def command(self, job_id: str) -> tuple[list[str], dict]:
        from .cli import _uv_binary

        pipeline_dir = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["PUBLIKCLIP_HOME"] = str(config.home_dir().resolve())
        # The pipeline bootstrap performs an exact sync. Isolate it so that
        # it cannot uninstall the SDK from the live MCP server environment.
        env["UV_PROJECT_ENVIRONMENT"] = str((self.root / "pipeline-env").resolve())
        return [
            _uv_binary(), "--directory", str(pipeline_dir), "run", "--frozen",
            "--group", "pipeline", "publikclip", "--jsonl", "resume", job_id,
        ], env

    def _provider_ready(self, provider: str) -> None:
        if provider == "chatgpt":
            from .chatgpt import status

            account = status()
            if not account["connected"] or not account["model"]:
                raise ValueError("Sign in with ChatGPT and select a model in Brain & keys or the CLI before clipping.")

    async def start(self, source: str, provider: Provider = "chatgpt",
                    captions: Caption = "classic", camera: Camera = "cut") -> dict:
        source_type, source = validate_source(source)
        if provider not in {"chatgpt", "ollama", "publik", "gemini"}:
            raise ValueError("Unknown scoring provider.")
        if captions not in {"classic", "beast", "hormozi", "minimal", "karaoke-pop"}:
            raise ValueError("Unknown caption preset.")
        if camera not in {"cut", "pan", "locked"}:
            raise ValueError("Unknown camera mode.")
        async with self.guard:
            self._idle()
            self._provider_ready(provider)
            settings = config.Settings(llm_mode=provider, caption_preset=captions)
            settings.camera.speaker_change = camera
            job = queue.create_job(source_type, source, json.dumps(settings.to_json()))
            await self._launch(job)
            return self.status(job.id)

    def _idle(self) -> None:
        if self.active:
            raise ValueError("A video job is already running in this MCP server; wait or cancel it first.")

    async def resume(self, job_id: str) -> dict:
        async with self.guard:
            self._idle()
            job = job_for(job_id)
            if job.status == "running":
                raise ValueError("This job is marked running; stop its desktop/CLI worker before resuming it here.")
            settings = config.Settings.from_json(json.loads(job.settings_json))
            self._provider_ready(settings.llm_mode)
            # Resume keeps the saved provider and options, preserving checkpoints.
            await self._launch(job)
            return self.status(job.id)

    async def _launch(self, job: queue.Job) -> None:
        command, env = self.command(job.id)
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *command, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024, **kwargs,
            )
        except OSError as err:
            queue.set_job_status(job.id, "failed", "Could not start the worker. Install uv and retry.")
            raise ValueError("Could not start the worker. Install uv and retry.") from err
        run = RunningJob(process)
        self.active[job.id] = run
        queue.set_job_status(job.id, "running")
        self._record(job.id, {"state": "running", "progress": run.progress})
        run.monitor = asyncio.create_task(self._monitor(job.id, run))

    async def _monitor(self, job_id: str, run: RunningJob) -> None:
        result: dict | None = None

        async def consume_stdout() -> None:
            nonlocal result
            assert run.process.stdout is not None
            async for line in run.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "progress":
                    run.progress = {key: event.get(key) for key in ("stage", "fraction", "message")}
                    self._record(job_id, {"state": "running", "progress": run.progress})
                elif event.get("event") == "result":
                    result = event

        async def drain_stderr() -> None:
            # Consume continuously to prevent a full pipe deadlocking uv/ffmpeg.
            # Never expose raw stderr or dependency logs through MCP.
            assert run.process.stderr is not None
            while await run.process.stderr.read(65536):
                pass

        state = "failed"
        try:
            await asyncio.gather(consume_stdout(), drain_stderr())
            code = await run.process.wait()
            if run.stopped:
                state = "cancelled"
                queue.set_job_status(job_id, "failed", "Cancelled from MCP. Resume to continue from checkpoints.")
            elif code == 0 and result and result.get("ok"):
                state = "done"
                queue.set_job_status(job_id, "done")
            else:
                error = (result or {}).get("error") or f"Video worker exited {code} without a successful result. Check local dependencies and resume."
                queue.set_job_status(job_id, "failed", str(error))
        except Exception:
            await self._terminate(run)
            queue.set_job_status(job_id, "failed", "Worker output could not be read. Resume from checkpoints.")
        finally:
            self._record(job_id, {"state": state, "progress": run.progress})
            self.active.pop(job_id, None)

    async def _terminate(self, run: RunningJob) -> None:
        if os.name == "nt":
            if run.process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(run.process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
                if run.process.returncode is None and killer.returncode:
                    run.process.kill()
        else:
            # uv, pipeline and ffmpeg share this dedicated process group.
            try:
                os.killpg(run.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            # Escalate for descendants too, even if the immediate child exits.
            await asyncio.sleep(0.25)
            try:
                os.killpg(run.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await run.process.wait()

    async def cancel(self, job_id: str) -> dict:
        job_for(job_id)
        run = self.active.get(job_id)
        if run is None:
            raise ValueError("No worker owned by this server is running for this job.")
        run.stopped = True
        await self._terminate(run)
        if run.monitor:
            await run.monitor
        return self.status(job_id)

    async def close(self) -> None:
        try:
            for job_id in list(self.active):
                await self.cancel(job_id)
        finally:
            self.instance_lock.release()

    def status(self, job_id: str) -> dict:
        job = job_for(job_id)
        history = self._history(job_id)
        run = self.active.get(job_id)
        state = job.status
        if state == "failed" and history.get("state") == "cancelled":
            state = "cancelled"
        return {
            "job_id": job.id, "status": state, "created_at": job.created_at,
            "source": job.source, "title": job.title, "error": job.error,
            "settings": json.loads(job.settings_json), "stages": queue.stage_statuses(job.id),
            "progress": run.progress if run else history.get("progress"),
            "worker_owned_by_server": run is not None, "directory": str(job.dir.resolve()),
        }

    def results(self, job_id: str) -> dict:
        job = job_for(job_id)
        score = read_stage(job, "score") or {}
        render = read_stage(job, "render") or {}
        outputs = []
        for item in render.get("outputs", []):
            path = Path(item["path"]).resolve()
            if not path.is_relative_to((job.dir / "clips").resolve()) or not path.is_relative_to(job.dir.resolve()):
                raise ValueError("Rendered file is outside this job's clips folder.")
            outputs.append({**item, "path": str(path), "exists": path.is_file(),
                            "bytes": path.stat().st_size if path.is_file() else None})
        return {"job": self.status(job_id), "scored_clips": score.get("clips", []),
                "rendered_clips": outputs, "captions_burned": render.get("captions_burned")}

    def transcript(self, job_id: str, offset: int = 0, limit: int = 100) -> dict:
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("Use offset >= 0 and limit between 1 and 500.")
        job = job_for(job_id)
        data = read_stage(job, "diarize") or read_stage(job, "asr") or {}
        segments = data.get("segments", [])
        end = min(len(segments), offset + limit)
        return {"job_id": job_id, "segments": segments[offset:end], "total": len(segments),
                "next_offset": end if end < len(segments) else None}
