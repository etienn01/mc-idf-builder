import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from app.patcher import apply as _patch

log = logging.getLogger(__name__)

MESHCORE_REPO = os.environ.get("MESHCORE_REPO", "https://github.com/meshcore-dev/MeshCore.git")
DOWNLOADS_DIR = Path("downloads")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_BUILDS", "2"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "10"))
DOWNLOAD_TTL_HOURS = int(os.environ.get("DOWNLOAD_TTL_HOURS", "1"))

_LORA_FLAGS = [
    "-D LORA_FREQ=869.618",
    "-D LORA_BW=62.5",
    "-D LORA_SF=8",
    "-D LORA_CR=8",
]



class BuildStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RegionEntry:
    name: str
    parent: str | None
    flood: Literal["allow", "deny"]


@dataclass
class BuildRequest:
    env: str
    ref: str
    prs: list[int] = field(default_factory=list)
    wifi_ssid: str = ""
    wifi_pwd: str = ""
    regions: list[RegionEntry] = field(default_factory=list)


@dataclass
class BuildJob:
    id: str
    env: str
    ref: str
    prs: list[int]
    build_flags: str
    status: BuildStatus = BuildStatus.PENDING
    log_lines: list[str] = field(default_factory=list)
    firmware_path: Path | None = None
    completed_at: float | None = None




def _encode_regions(regions: list[RegionEntry]) -> str:
    parts = []
    for r in regions:
        parent_part = f"/{r.parent}" if r.parent else ""
        flag = "A" if r.flood == "allow" else "D"
        parts.append(f"{r.name}{parent_part}:{flag}")
    # Join with \x3b (C hex escape for ';') so no literal semicolons appear
    # in the shell command line. GCC expands \x3b to ';' in the compiled string.
    return "\\x3b".join(parts)



def build_flags_for(req: BuildRequest) -> str:
    flags = list(_LORA_FLAGS)
    if req.wifi_ssid:
        flags.append(f"-D WIFI_SSID='\"{ req.wifi_ssid }\"'")
    if req.wifi_pwd:
        flags.append(f"-D WIFI_PWD='\"{ req.wifi_pwd }\"'")
    if req.regions:
        flags.append(f"-D DEFAULT_REGION_CFG='\"{ _encode_regions(req.regions) }\"'")
    return " ".join(flags)


class BuildQueue:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, BuildJob] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, job: BuildJob) -> None:
        self._jobs[job.id] = job
        task = asyncio.create_task(self._guarded_run(job))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.id, None))

    def get(self, job_id: str) -> BuildJob | None:
        return self._jobs.get(job_id)

    def queue_depth(self) -> int:
        return sum(
            1 for j in self._jobs.values()
            if j.status in (BuildStatus.PENDING, BuildStatus.RUNNING)
        )

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    def _cleanup(self) -> None:
        cutoff = time.time() - DOWNLOAD_TTL_HOURS * 3600
        if DOWNLOADS_DIR.exists():
            for entry in DOWNLOADS_DIR.iterdir():
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
        for job_id, job in list(self._jobs.items()):
            if (
                job.status not in (BuildStatus.PENDING, BuildStatus.RUNNING)
                and job.completed_at is not None
                and job.completed_at < cutoff
            ):
                self._jobs.pop(job_id, None)

    async def _emit(self, job_id: str, line: str | None) -> None:
        if line is not None:
            log = self._jobs[job_id].log_lines
            log.append(line)
            if len(log) > 2000:
                del log[0]
        for q in list(self._subscribers.get(job_id, [])):
            await q.put(line)

    async def _guarded_run(self, job: BuildJob) -> None:
        await self._emit(job.id, "Queued, waiting for a build slot…")
        async with self._sem:
            await self._run_job(job)

    async def _run_job(self, job: BuildJob) -> None:
        job.status = BuildStatus.RUNNING
        tmpdir = tempfile.mkdtemp(prefix=f"meshcore-{job.id[:8]}-")
        srcdir = os.path.join(tmpdir, "repo")

        async def run(*args: str, cwd: str = srcdir, env: dict[str, str] | None = None) -> int:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env or os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                chunk = raw.decode(errors="replace")
                # \r is used for in-place progress (e.g. git clone); keep only
                # the final state of each such group rather than flooding the log.
                parts = [p.rstrip() for p in chunk.split('\r') if p.strip()]
                if parts:
                    await self._emit(job.id, parts[-1])
            await proc.wait()
            assert proc.returncode is not None
            return proc.returncode

        # Git identity required for merge commits
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "builder", "GIT_AUTHOR_EMAIL": "builder@meshcore",
            "GIT_COMMITTER_NAME": "builder", "GIT_COMMITTER_EMAIL": "builder@meshcore",
        }

        try:
            await self._emit(job.id, f"=== Cloning MeshCore @ {job.ref} ===")
            rc = await run(
                "git", "clone", "--depth", "1", "--branch", job.ref,
                "--progress", MESHCORE_REPO, srcdir,
                cwd=tmpdir,
            )
            if rc != 0:
                raise RuntimeError(f"git clone failed (exit {rc})")

            for pr in job.prs:
                await self._emit(job.id, f"=== Merging PR #{pr} ===")
                rc = await run("git", "fetch", "--depth", "1", "origin",
                               f"refs/pull/{pr}/head", env=git_env)
                if rc != 0:
                    raise RuntimeError(f"git fetch failed for PR #{pr}")

                # Deepen both sides until the merge base is reachable
                depth = 1
                while True:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "merge-base", "HEAD", "FETCH_HEAD",
                        cwd=srcdir, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    if proc.returncode == 0:
                        break
                    depth *= 2
                    if depth > 4096:
                        raise RuntimeError(f"Could not find merge base for PR #{pr}")
                    await run("git", "fetch", "--deepen", str(depth), "origin",
                              f"refs/pull/{pr}/head", env=git_env)
                    await run("git", "fetch", "--deepen", str(depth), "origin",
                              job.ref, env=git_env)

                rc = await run("git", "merge", "--no-edit", "FETCH_HEAD", env=git_env)
                if rc != 0:
                    raise RuntimeError(f"PR #{pr} has conflicts with the current tree")

            await self._emit(job.id, "=== Applying patch ===")
            _patch(srcdir)
            await self._emit(job.id, "Patch applied.")

            # Build version label for the output filename
            sha_proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--short", "HEAD",
                cwd=srcdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            sha_out, _ = await sha_proc.communicate()
            sha = sha_out.decode().strip()

            _tag_prefix = "repeater-"
            base_label = job.ref[len(_tag_prefix):] if job.ref.startswith(_tag_prefix) else job.ref
            pr_suffix = "".join(f"+pr{p}" for p in job.prs)
            if job.prs or not job.ref.startswith(_tag_prefix):
                version_label = f"{base_label}{pr_suffix}-{sha}"
            else:
                version_label = base_label

            await self._emit(job.id, f"=== Building {job.env} ===")
            build_env = {**os.environ, "PLATFORMIO_BUILD_FLAGS": job.build_flags}
            rc = await run("pio", "run", "-e", job.env, env=build_env)
            if rc != 0:
                raise RuntimeError(f"pio run exited with code {rc}")

            build_dir = Path(srcdir) / ".pio" / "build" / job.env
            src = next(
                (build_dir / name for name in ("firmware.bin", "firmware.zip", "firmware.hex")
                 if (build_dir / name).exists()),
                None,
            )
            if src is None:
                raise RuntimeError("No firmware binary found after build")

            dest_dir = DOWNLOADS_DIR / job.id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{job.env}-{version_label}{src.suffix}"
            shutil.copy2(src, dest)

            job.firmware_path = dest
            job.status = BuildStatus.COMPLETED
            await self._emit(job.id, f"=== Build complete: {dest.name} ===")
            log.info("[%s] completed: %s", job.id, dest.name)

        except asyncio.CancelledError:
            job.status = BuildStatus.CANCELLED
            await self._emit(job.id, "=== Build cancelled ===")
            await self._emit(job.id, None)
            log.info("[%s] cancelled", job.id)
            raise
        except Exception as exc:
            job.status = BuildStatus.FAILED
            await self._emit(job.id, f"=== Build failed: {exc} ===")
            log.error("[%s] failed: %s", job.id, exc)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            job.completed_at = time.time()
            if job.status != BuildStatus.CANCELLED:
                await self._emit(job.id, None)
            self._cleanup()


queue = BuildQueue()


def make_job(req: BuildRequest) -> BuildJob:
    return BuildJob(
        id=str(uuid.uuid4()),
        env=req.env,
        ref=req.ref,
        prs=list(req.prs),
        build_flags=build_flags_for(req),
    )
