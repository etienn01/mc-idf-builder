"""FastAPI web application for the MeshCore firmware builder."""
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.builder import (
    MAX_QUEUE_SIZE,
    MESHCORE_REPO,
    BuildRequest,
    BuildStatus,
    RegionEntry,
    make_job,
    queue,
)
from app.parser import group_by_folder, load_environments, prettify_board_name

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

_envs = []
_versions_cache: list[str] = []
_versions_cache_time: float = 0.0

_REPEATER_TAG_RE = re.compile(r"^repeater-(v\d+\.\d+.*)$")
_MIN_VERSION = (1, 10, 0)


async def _clone_for_discovery() -> str:
    ref = os.environ.get("MESHCORE_REF", "dev")
    tmpdir = tempfile.mkdtemp(prefix="meshcore-discovery-")
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, MESHCORE_REPO, tmpdir,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed (exit {proc.returncode})")
    return tmpdir


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _envs
    tmpdir = None
    try:
        tmpdir = await _clone_for_discovery()
        _envs = load_environments(tmpdir)
    except Exception as exc:
        print(f"WARNING: failed to load environments: {exc}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    yield
    queue.cancel_all()


app = FastAPI(title="MeshCore Firmware Builder", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


_SAFE_STR = r'^[^"\\]*$'

class RegionEntryModel(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9\-\$\#]{1,30}$")
    parent: str | None = None
    flood: Literal["allow", "deny"]


class BuildRequestModel(BaseModel):
    env: str
    ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]{0,99}$")
    prs: list[Annotated[int, Field(gt=0)]] = Field(default=[], max_length=10)
    advert_name: str = Field("", max_length=31, pattern=_SAFE_STR)
    admin_password: str = Field("", max_length=15, pattern=_SAFE_STR)
    advert_lat: float | None = Field(None, ge=-90, le=90)
    advert_lon: float | None = Field(None, ge=-180, le=180)
    wifi_ssid: str = Field("", max_length=32, pattern=_SAFE_STR)
    wifi_pwd: str = Field("", max_length=63, pattern=_SAFE_STR)
    regions: list[RegionEntryModel] = []

    @field_validator("prs")
    @classmethod
    def deduplicate_prs(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/api/environments")
def get_environments():
    grouped = group_by_folder(_envs)
    return [
        {
            "id": board_id,
            "name": prettify_board_name(board_id),
            "envs": [
                {"env_name": e.env_name, "role": e.role, "label": e.label, "platform": e.platform}
                for e in envs
            ],
        }
        for board_id, envs in grouped.items()
    ]


@app.get("/api/versions")
async def get_versions():
    global _versions_cache, _versions_cache_time
    if time.monotonic() - _versions_cache_time < 300 and _versions_cache:
        return _versions_cache

    proc = await asyncio.create_subprocess_exec(
        "git", "ls-remote", "--tags", "--heads", MESHCORE_REPO,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"git ls-remote failed: {stderr.decode().strip()}")

    # Build ref → commit SHA map; prefer ^{} (dereferenced) SHA for annotated tags
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
    _versions_cache = tags[:20] + branches
    _versions_cache_time = time.monotonic()
    return _versions_cache


@app.post("/api/build")
async def submit_build(body: BuildRequestModel):
    if body.env not in {e.env_name for e in _envs}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown environment: {body.env!r}")
    if queue.queue_depth() >= MAX_QUEUE_SIZE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Build queue is full, try again later")  # noqa: E501

    req = BuildRequest(
        env=body.env,
        ref=body.ref,
        prs=body.prs,
        advert_name=body.advert_name,
        admin_password=body.admin_password,
        advert_lat=body.advert_lat,
        advert_lon=body.advert_lon,
        wifi_ssid=body.wifi_ssid,
        wifi_pwd=body.wifi_pwd,
        regions=[
            RegionEntry(name=r.name, parent=r.parent, flood=r.flood)
            for r in body.regions
        ],
    )
    job = make_job(req)
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
