"""Concurrent build queue: isolated clone per build, shared pio package cache."""
import asyncio
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

MESHCORE_REPO = os.environ.get("MESHCORE_REPO", "https://github.com/meshcore-dev/MeshCore.git")
DOWNLOADS_DIR = Path("downloads")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_BUILDS", "3"))

_LORA_FLAGS = [
    "-D LORA_FREQ=869.618",
    "-D LORA_BW=62.5",
    "-D LORA_SF=8",
    "-D LORA_CR=8",
]

_REGION_NAME_RE = re.compile(r"^[a-z0-9\-\$\#]+$")
_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]{0,99}$")


class BuildStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class RegionEntry:
    name: str
    parent: Optional[str]
    flood: str  # "allow" | "deny"


@dataclass
class BuildRequest:
    env: str
    ref: str = "main"
    advert_name: str = ""
    admin_password: str = ""
    advert_lat: Optional[float] = None
    advert_lon: Optional[float] = None
    wifi_ssid: str = ""
    wifi_pwd: str = ""
    regions: list[RegionEntry] = field(default_factory=list)


@dataclass
class BuildJob:
    id: str
    env: str
    ref: str
    build_flags: str
    status: BuildStatus = BuildStatus.PENDING
    log_lines: list[str] = field(default_factory=list)
    firmware_path: Optional[Path] = None


def _sanitize(s: str, max_len: int) -> str:
    return re.sub(r'["\\]', "", s)[:max_len]


def _encode_regions(regions: list[RegionEntry]) -> str:
    parts = []
    for r in regions:
        parent_part = f"/{r.parent}" if r.parent else ""
        flag = "A" if r.flood == "allow" else "D"
        parts.append(f"{r.name}{parent_part}:{flag}")
    # Join with \x3b (C hex escape for ';') so no literal semicolons appear
    # in the shell command line. GCC expands \x3b to ';' in the compiled string.
    return "\\x3b".join(parts)


def validate_region_name(name: str) -> bool:
    return bool(_REGION_NAME_RE.match(name)) and len(name) <= 30


def validate_ref(ref: str) -> bool:
    return bool(_REF_RE.match(ref))


def build_flags_for(req: BuildRequest) -> str:
    flags = list(_LORA_FLAGS)
    if req.advert_name:
        safe_name = _sanitize(req.advert_name, 31)
        flags.append(f"-D ADVERT_NAME='\"{ safe_name }\"'")
    if req.admin_password:
        safe_pw = _sanitize(req.admin_password, 15)
        flags.append(f"-D ADMIN_PASSWORD='\"{ safe_pw }\"'")
    if req.advert_lat is not None:
        flags.append(f"-D ADVERT_LAT={req.advert_lat:.6f}")
    if req.advert_lon is not None:
        flags.append(f"-D ADVERT_LON={req.advert_lon:.6f}")
    if req.wifi_ssid:
        safe_ssid = _sanitize(req.wifi_ssid, 32)
        flags.append(f"-D WIFI_SSID='\"{ safe_ssid }\"'")
    if req.wifi_pwd:
        safe_pwd = _sanitize(req.wifi_pwd, 63)
        flags.append(f"-D WIFI_PWD='\"{ safe_pwd }\"'")
    if req.regions:
        cfg = _encode_regions(req.regions)
        flags.append(f"-D DEFAULT_REGION_CFG='\"{ cfg }\"'")
    return " ".join(flags)


class BuildQueue:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, BuildJob] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._tasks: set[asyncio.Task] = set()

    def submit(self, job: BuildJob) -> None:
        self._jobs[job.id] = job
        task = asyncio.create_task(self._guarded_run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def get(self, job_id: str) -> Optional[BuildJob]:
        return self._jobs.get(job_id)

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(job_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def _emit(self, job_id: str, line: Optional[str]) -> None:
        if line is not None:
            self._jobs[job_id].log_lines.append(line)
        for q in list(self._subscribers.get(job_id, [])):
            await q.put(line)

    async def _guarded_run(self, job: BuildJob) -> None:
        async with self._sem:
            await self._run_job(job)

    async def _run_job(self, job: BuildJob) -> None:
        job.status = BuildStatus.RUNNING
        tmpdir = tempfile.mkdtemp(prefix=f"meshcore-{job.id[:8]}-")
        srcdir = os.path.join(tmpdir, "repo")

        async def run(*args, cwd: str = srcdir, env: dict = None) -> int:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env or os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for raw in proc.stdout:
                chunk = raw.decode(errors="replace")
                # \r is used for in-place progress (e.g. git clone); keep only
                # the final state of each such group rather than flooding the log.
                parts = [p.rstrip() for p in chunk.split('\r') if p.strip()]
                if parts:
                    await self._emit(job.id, parts[-1])
            await proc.wait()
            return proc.returncode

        try:
            await self._emit(job.id, f"=== Cloning MeshCore @ {job.ref} ===")
            rc = await run(
                "git", "clone", "--depth", "1", "--branch", job.ref,
                "--progress", MESHCORE_REPO, srcdir,
                cwd=tmpdir,
            )
            if rc != 0:
                raise RuntimeError(f"git clone failed (exit {rc})")

            await self._emit(job.id, "=== Applying patch ===")
            from app.patcher import apply as patch
            patch(srcdir)
            await self._emit(job.id, "Patch applied.")

            # Tags are "repeater-vX.Y.Z"; branches get the SHA for traceability
            _tag_prefix = "repeater-"
            if job.ref.startswith(_tag_prefix):
                version_label = job.ref[len(_tag_prefix):]
            else:
                sha_proc = await asyncio.create_subprocess_exec(
                    "git", "rev-parse", "--short", "HEAD",
                    cwd=srcdir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                sha_out, _ = await sha_proc.communicate()
                sha = sha_out.decode().strip()
                version_label = f"{job.ref}-{sha}" if sha else job.ref

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

        except Exception as exc:
            job.status = BuildStatus.FAILED
            await self._emit(job.id, f"=== Build failed: {exc} ===")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            await self._emit(job.id, None)


queue = BuildQueue()


def make_job(req: BuildRequest) -> BuildJob:
    return BuildJob(
        id=str(uuid.uuid4()),
        env=req.env,
        ref=req.ref,
        build_flags=build_flags_for(req),
    )
