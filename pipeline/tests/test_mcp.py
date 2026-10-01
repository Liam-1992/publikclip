"""Real MCP transport tests and subprocess lifecycle tests without AI/model downloads."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn
from filelock import Timeout
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from publikclip_pipeline import config
from publikclip_pipeline.jobs import queue
from publikclip_pipeline.mcp_jobs import JobManager, RunningJob, job_for, validate_source
from publikclip_pipeline.mcp_server import create_http_app

PIPELINE = Path(__file__).resolve().parents[1]
TOOLS = {"server_status", "list_jobs", "start_job", "resume_job", "job_status",
         "cancel_job", "job_results", "get_transcript"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("PUBLIKCLIP_HOME", str(root))
    return root


def new_job():
    return queue.create_job("file", "fixture.mp4", json.dumps(config.Settings(llm_mode="ollama").to_json()))


async def wait_done(manager, job_id):
    await asyncio.wait_for(manager.active[job_id].monitor, timeout=15)
    return manager.status(job_id)


def test_worker_lifecycle_and_results(home, tmp_path, monkeypatch):
    """Launch a real worker, drain a >pipe-capacity stderr, and read its persisted result."""
    worker = tmp_path / "worker.py"
    worker.write_text('''
import json,sys
from publikclip_pipeline.jobs import queue
job=queue.get_job(sys.argv[1])
sys.stderr.write("private diagnostic\\n" * 20000)
print(json.dumps({"event":"progress","stage":"render","fraction":0.5,"message":"Rendering fixture"}),flush=True)
out=job.dir/"clips"/"clip_00.mp4"
out.parent.mkdir()
out.write_bytes(b"fixture-render-output")
queue.write_checkpoint(job,"score",1,{"clips":[{"start":0,"end":1,"score":80}]})
queue.write_checkpoint(job,"render",1,{"outputs":[{"clip":0,"path":str(out)}],"captions_burned":True})
queue.write_checkpoint(job,"diarize",1,{"segments":[{"text":"hello"},{"text":"world"}]})
print(json.dumps({"event":"result","ok":True,"job_id":job.id}),flush=True)
''', encoding="utf-8")
    source = tmp_path / "video with spaces.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
        [sys.executable, str(worker), job_id], os.environ.copy()))

    async def scenario():
        manager = JobManager()
        manager.acquire()
        try:
            job = await manager.start(str(source), provider="ollama", captions="minimal", camera="locked")
            result = await wait_done(manager, job["job_id"])
            assert result["status"] == "done"
            assert result["progress"]["message"] == "Rendering fixture"
            assert result["settings"]["llm_mode"] == "ollama"
            assert result["settings"]["camera"]["speaker_change"] == "locked"
            output = manager.results(job["job_id"])
            assert output["rendered_clips"][0]["bytes"] == 21
            assert output["scored_clips"][0]["score"] == 80
            assert manager.transcript(job["job_id"], limit=1)["next_offset"] == 1
            assert manager.transcript(job["job_id"], offset=1)["segments"] == [{"text": "world"}]
            assert "private diagnostic" not in json.dumps(output)
        finally:
            await manager.close()
        # Results and progress survive reconnect/restart.
        fresh = JobManager()
        assert fresh.status(job["job_id"])["status"] == "done"
        assert fresh.results(job["job_id"])["rendered_clips"][0]["exists"]
    asyncio.run(scenario())


def test_cancel_resume_failure_and_shutdown(home, tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
        [sys.executable, "-c", "import time; time.sleep(120)"], os.environ.copy()))

    async def scenario():
        manager = JobManager()
        manager.acquire()
        job = await manager.start(str(source), provider="ollama")
        job_id = job["job_id"]
        with pytest.raises(ValueError, match="already running"):
            await manager.start(str(source), provider="ollama")
        assert (await manager.cancel(job_id))["status"] == "cancelled"
        assert not manager.active
        monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
            [sys.executable, "-c", "raise SystemExit(7)"], os.environ.copy()))
        await manager.resume(job_id)
        result = await wait_done(manager, job_id)
        assert result["status"] == "failed"
        assert "exited 7" in result["error"]
        monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
            [sys.executable, "-c", "import time; time.sleep(120)"], os.environ.copy()))
        await manager.resume(job_id)
        await manager.close()
        assert manager.status(job_id)["status"] == "cancelled"
        assert not manager.active
    asyncio.run(scenario())


def test_cancelled_worker_pipe_reset_remains_cancelled(home):
    class ResetStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionResetError("pipe closed by process-tree termination")

        async def read(self, size):
            return b""

    class KilledProcess:
        stdout = ResetStream()
        stderr = ResetStream()

        async def wait(self):
            return 1

    async def scenario():
        manager = JobManager()
        job = new_job()
        run = RunningJob(KilledProcess(), stopped=True)
        manager.active[job.id] = run
        await manager._monitor(job.id, run)
        assert manager.status(job.id)["status"] == "cancelled"
        assert "Cancelled from MCP" in manager.status(job.id)["error"]
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group escalation")
def test_cancel_kills_descendant_ignoring_sigterm(home, tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"
    code = ("import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c'," + repr(child_code)
            + "]); pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid)); time.sleep(120)")
    monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
        [sys.executable, "-c", code], os.environ.copy()))

    async def scenario():
        manager = JobManager()
        job = new_job()
        await manager._launch(job)
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        assert pidfile.exists()
        await asyncio.sleep(0.1)  # allow child to install its SIGTERM handler
        await manager.cancel(job.id)
        pid = int(pidfile.read_text())
        proc_stat = Path(f"/proc/{pid}/stat")
        assert not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z"
    asyncio.run(scenario())


def test_source_ids_checkpoints_and_instance_lock(home, tmp_path):
    assert validate_source("https://youtu.be/abc")[0] == "url"
    for source in ["https://127.0.0.1/test", "http://youtube.com/test", "https://youtube.com.evil/test",
                   "https://user:pass@youtube.com/test", "https://youtube.com:99/test", "", str(home / "secrets.json")]:
        with pytest.raises(ValueError):
            validate_source(source)
    with pytest.raises(ValueError):
        job_for("../../chatgpt")
    manager = JobManager()
    manager.acquire()
    other = JobManager()
    try:
        with pytest.raises(Timeout):
            other.acquire()
        job = new_job()
        with pytest.raises(ValueError):
            manager.transcript(job.id, limit=0)
        queue.write_checkpoint(job, "render", 1, {"outputs": [{"path": str(home / "secrets.json")}]})
        with pytest.raises(ValueError, match="outside"):
            manager.results(job.id)
        (job.dir / "render.json").write_text("broken")
        with pytest.raises(ValueError, match="checkpoint is invalid"):
            manager.results(job.id)
    finally:
        manager.instance_lock.release()


def test_worker_environment_is_isolated(home):
    manager = JobManager()
    command, env = manager.command("20261001-000000-abcdef")
    assert "--group" in command and "pipeline" in command and "--frozen" in command
    assert env["UV_PROJECT_ENVIRONMENT"] == str((home / "mcp" / "pipeline-env").resolve())
    assert command[-3:] == ["--jsonl", "resume", "20261001-000000-abcdef"]


def test_stdio_protocol_and_resource(home, tmp_path):
    job = new_job()
    queue.write_checkpoint(job, "score", 1, {"clips": [{"score": 75}]})
    queue.set_job_status(job.id, "done")
    source = tmp_path / "video.mp4"
    source.write_bytes(b"source")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "publikclip_pipeline.mcp_server"],
        env={**os.environ, "PYTHONPATH": str(PIPELINE)})

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "PublikClip"
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == TOOLS
                start = next(tool for tool in listed.tools if tool.name == "start_job")
                assert start.inputSchema["properties"]["provider"]["default"] == "chatgpt"
                assert start.annotations.openWorldHint
                status = await client.call_tool("server_status", {})
                assert not status.isError
                assert not status.structuredContent["chatgpt"]["connected"]
                jobs = await client.call_tool("list_jobs", {})
                assert jobs.structuredContent["jobs"][0]["job_id"] == job.id
                results = await client.call_tool("job_results", {"job_id": job.id})
                assert results.structuredContent["scored_clips"][0]["score"] == 75
                resource = await client.read_resource(f"publikclip://jobs/{job.id}/results")
                assert json.loads(resource.contents[0].text)["job"]["job_id"] == job.id
                bad = await client.call_tool("start_job", {"source": str(source)})
                assert bad.isError and "Sign in with ChatGPT" in bad.content[0].text
                invalid = await client.call_tool("job_status", {"job_id": "../secrets"})
                assert invalid.isError
    asyncio.run(scenario())


def test_http_protocol_and_dns_rebinding(home):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(PIPELINE)}
    process = subprocess.Popen([sys.executable, "-m", "publikclip_pipeline.mcp_server",
                               "--transport", "streamable-http", "--port", str(port)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def scenario():
        url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(100):
                try:
                    health = await http.get(url + "/health")
                    if health.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("HTTP MCP server did not start")
            assert health.json()["ok"]
            invalid_host = await http.post(url + "/mcp", headers={"Host": "evil.example"}, json={})
            assert invalid_host.status_code == 421
            invalid_origin = await http.post(url + "/mcp", headers={"Origin": "https://evil.example"}, json={})
            assert invalid_origin.status_code == 403
        async with httpx.AsyncClient(trust_env=False) as transport_http, streamable_http_client(
                url + "/mcp", http_client=transport_http) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert {t.name for t in (await client.list_tools()).tools} == TOOLS
                result = await client.call_tool("list_jobs", {})
                assert result.structuredContent == {"jobs": []}
    try:
        asyncio.run(scenario())
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_http_worker_survives_responses_and_reconnect(home, tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(JobManager, "command", lambda self, job_id: (
        [sys.executable, "-c", "import time; time.sleep(120)"], os.environ.copy()))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async def scenario():
        manager = JobManager()
        server = uvicorn.Server(uvicorn.Config(create_http_app(port, manager), host="127.0.0.1",
                                               port=port, log_level="error"))
        serve = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            async with httpx.AsyncClient(trust_env=False) as http:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("start_job", {"source": str(source), "provider": "ollama"})
                        assert not result.isError
                        job_id = result.structuredContent["job_id"]
                        state = await client.call_tool("job_status", {"job_id": job_id})
                        assert state.structuredContent["worker_owned_by_server"]
                assert job_id in manager.active  # client disconnect must not kill worker
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        cancelled = await client.call_tool("cancel_job", {"job_id": job_id})
                        assert cancelled.structuredContent["status"] == "cancelled"
                        resumed = await client.call_tool("resume_job", {"job_id": job_id})
                        assert resumed.structuredContent["worker_owned_by_server"]
        finally:
            server.should_exit = True
            await asyncio.wait_for(serve, timeout=10)
        assert not manager.active
        assert manager.status(job_id)["status"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_video_artifact(home):
    """An actual MP4 can be returned as a verified local artifact, without model inference."""
    job = new_job()
    out = job.dir / "clips" / "clip_00.mp4"
    out.parent.mkdir()
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=108x192:r=10", "-t", "0.3", "-c:v", "libx264", str(out)], check=True)
    queue.write_checkpoint(job, "render", 1, {"outputs": [{"clip": 0, "path": str(out)}]})
    result = JobManager().results(job.id)["rendered_clips"][0]
    assert result["exists"] and result["bytes"] > 1000
