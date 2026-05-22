"""FastAPI web application for the MeshCore firmware builder."""
import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.builder import (
    MAX_QUEUE_SIZE,
    BuildRequest,
    BuildStatus,
    RegionEntry,
    make_job,
    queue,
)
from app.parser import group_by_folder, load_environments, prettify_board_name

_SOURCES: dict[str, dict] = {
    "official": {
        "repo": "https://github.com/meshcore-dev/MeshCore.git",
        "default_ref": "dev",
    },
    "mqtt_fork": {
        "repo": "https://github.com/agessaman/MeshCore.git",
        "default_ref": "mqtt-bridge-implementation-flex",
    },
}

_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_app_logger.propagate = False
_app_logger.addHandler(logging.StreamHandler())
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

_envs_cache: dict[str, list] = {}
_envs_cache_time: dict[str, float] = {}
_envs_locks: dict[str, asyncio.Lock] = {}
_versions_cache: dict[str, list] = {}
_versions_cache_time: dict[str, float] = {}

_REPEATER_TAG_RE = re.compile(r"^repeater-(v\d+\.\d+.*)$")
_MIN_VERSION = (1, 10, 0)


async def _clone_for_discovery(repo: str, ref: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="meshcore-discovery-")
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, repo, tmpdir,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed (exit {proc.returncode})")
    return tmpdir


async def _get_envs(source: str, ref: str) -> list:
    cache_key = f"{source}:{ref}"
    cached = _envs_cache.get(cache_key)
    if cached and time.monotonic() - _envs_cache_time.get(cache_key, 0) < 3600:
        return cached
    lock = _envs_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _envs_cache.get(cache_key)
        if cached and time.monotonic() - _envs_cache_time.get(cache_key, 0) < 3600:
            return cached
        tmpdir = None
        try:
            tmpdir = await _clone_for_discovery(_SOURCES[source]["repo"], ref)
            envs = load_environments(tmpdir)
            _envs_cache[cache_key] = envs
            _envs_cache_time[cache_key] = time.monotonic()
            return envs
        except Exception as exc:
            log.warning("failed to load environments for %s@%s: %s", source, ref, exc)
            return []
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _get_envs("official", _SOURCES["official"]["default_ref"])
    yield
    queue.cancel_all()


app = FastAPI(title="MeshCore Firmware Builder", lifespan=lifespan, docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


_SAFE_STR = r'^[^"\\]*$'

_REGION_NAME_PAT = r"^[a-zA-Z0-9\-\$\#]{1,30}$"

class RegionEntryModel(BaseModel):
    name: str = Field(pattern=_REGION_NAME_PAT)
    parent: str | None = Field(None, pattern=_REGION_NAME_PAT)
    flood: Literal["allow", "deny"]


class BuildRequestModel(BaseModel):
    source: Literal["official", "mqtt_fork"] = "official"
    env: str
    ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]{0,99}$")
    prs: list[Annotated[int, Field(gt=0)]] = Field(default=[], max_length=10)
    wifi_ssid: str = Field("", max_length=32, pattern=_SAFE_STR)
    wifi_pwd: str = Field("", max_length=63, pattern=_SAFE_STR)
    regions: list[RegionEntryModel] = []

    @field_validator("prs")
    @classmethod
    def deduplicate_prs(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def check_region_parents(self) -> "BuildRequestModel":
        names = {r.name for r in self.regions}
        for r in self.regions:
            if r.parent is None:
                continue
            if r.parent == r.name:
                raise ValueError(f"Region {r.name!r} cannot be its own parent")
            if r.parent not in names:
                raise ValueError(f"Region parent {r.parent!r} does not exist in regions list")
        return self


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/api/environments")
async def get_environments(source: str = "official", ref: str = ""):
    if source not in _SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown source: {source!r}")
    envs = await _get_envs(source, ref or _SOURCES[source]["default_ref"])
    if source == "mqtt_fork":
        envs = [e for e in envs if e.env_name.lower().endswith("_repeater_observer_mqtt")]
    grouped = group_by_folder(envs)
    return [
        {
            "id": board_id,
            "name": prettify_board_name(board_id),
            "envs": [
                {"env_name": e.env_name, "role": e.role, "label": e.label, "platform": e.platform}
                for e in board_envs
            ],
        }
        for board_id, board_envs in grouped.items()
    ]


@app.get("/api/versions")
async def get_versions(source: str = "official"):
    if source not in _SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown source: {source!r}")
    cached = _versions_cache.get(source)
    if cached and time.monotonic() - _versions_cache_time.get(source, 0) < 3600:
        return cached

    repo = _SOURCES[source]["repo"]
    default_ref = _SOURCES[source]["default_ref"]
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-remote", "--tags", "--heads", repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("git ls-remote failed for %s: %s", source, stderr.decode().strip())

    sha_map: dict[str, str] = {}
    for line in stdout.decode().splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref.endswith("^{}"):
            sha_map[ref[:-3]] = sha
        elif ref not in sha_map:
            sha_map[ref] = sha

    if source == "official":
        branches = []
        for branch in ("main", "dev"):
            ref = f"refs/heads/{branch}"
            if ref in sha_map:
                branches.append({"label": f"{branch} ({sha_map[ref][:7]})", "value": branch})

        tags = []
        for ref, sha in sha_map.items():
            if not ref.startswith("refs/tags/"):
                continue
            raw_tag = ref.removeprefix("refs/tags/")
            m = _REPEATER_TAG_RE.match(raw_tag)
            if m:
                tags.append({"label": f"{m.group(1)} ({sha[:7]})", "value": raw_tag})

        def _ver_key(t: dict):
            return tuple(int(x) for x in re.findall(r"\d+", t["label"]))

        tags = [t for t in tags if _ver_key(t) >= _MIN_VERSION]
        tags.sort(key=_ver_key, reverse=True)
        result = tags[:20] + branches
    else:
        branches = []
        for ref, sha in sha_map.items():
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
                branches.append({"label": f"{branch} ({sha[:7]})", "value": branch})
        branches.sort(key=lambda b: (0 if b["value"] == default_ref else 1, b["value"]))
        result = branches

    _versions_cache[source] = result
    _versions_cache_time[source] = time.monotonic()
    return result


@app.post("/api/build")
async def submit_build(body: BuildRequestModel):
    if body.source not in _SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown source: {body.source!r}")
    source_envs = await _get_envs(body.source, body.ref)
    if body.env not in {e.env_name for e in source_envs}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown environment: {body.env!r}")
    if queue.queue_depth() >= MAX_QUEUE_SIZE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Build queue is full, try again later")  # noqa: E501

    req = BuildRequest(
        env=body.env,
        ref=body.ref,
        repo=_SOURCES[body.source]["repo"],
        prs=body.prs,
        wifi_ssid=body.wifi_ssid,
        wifi_pwd=body.wifi_pwd,
        regions=[
            RegionEntry(name=r.name, parent=r.parent, flood=r.flood)
            for r in body.regions
        ],
    )
    job = make_job(req)
    log.info(
        "[%s] submitted source=%s env=%s ref=%s prs=%s regions=%s wifi=%s",
        job.id, body.source, job.env, job.ref, job.prs,
        [(r.name, r.parent, r.flood) for r in req.regions],
        bool(req.wifi_ssid),
    )
    queue.submit(job)
    return {"build_id": job.id}


@app.get("/api/builds/{build_id}")
def get_build(build_id: str):
    job = queue.get(build_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return {
        "build_id": job.id,
        "env": job.env,
        "ref": job.ref,
        "status": job.status,
        "download_url": f"/api/builds/{job.id}/download"
            if job.status == BuildStatus.COMPLETED else None,
        "filename": job.firmware_path.name if job.firmware_path else None,
    }


@app.delete("/api/builds/{build_id}")
async def cancel_build(build_id: str):
    job = queue.get(build_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if job.status not in (BuildStatus.PENDING, BuildStatus.RUNNING):
        raise HTTPException(status.HTTP_409_CONFLICT, "Build is not cancellable")
    queue.cancel(build_id)
    log.info("[%s] cancel requested", build_id)
    return {"build_id": build_id}


@app.get("/api/builds/{build_id}/logs")
async def stream_logs(build_id: str):
    job = queue.get(build_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    async def generate():
        for line in list(job.log_lines):
            yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"

        if job.status in (BuildStatus.COMPLETED, BuildStatus.FAILED):
            yield "data: [DONE]\n\n"
            return

        sub = queue.subscribe(build_id)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(sub.get(), timeout=20)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if line is None:
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
        finally:
            queue.unsubscribe(build_id, sub)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/builds/{build_id}/download")
def download_firmware(build_id: str):
    job = queue.get(build_id)
    if not job or job.status != BuildStatus.COMPLETED or not job.firmware_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not job.firmware_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(
        job.firmware_path,
        media_type="application/octet-stream",
        filename=job.firmware_path.name,
    )


# ---------------------------------------------------------------------------
# Static files (must come last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
