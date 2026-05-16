"""FastAPI web application for the MeshCore firmware builder."""
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from app.builder import (
    BuildJob,
    BuildRequest,
    BuildStatus,
    MAX_QUEUE_SIZE,
    MESHCORE_REPO,
    RegionEntry,
    make_job,
    queue,
    validate_ref,
    validate_region_name,
)
from app.parser import group_by_board, load_environments

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

_envs = []
_env_names: set[str] = set()
_versions_cache: list[str] = []
_versions_cache_time: float = 0.0


async def _clone_for_discovery() -> str:
    """Shallow-clone MeshCore into a temp dir and return the path."""
    ref = os.environ.get("MESHCORE_REF", "main")
    tmpdir = tempfile.mkdtemp(prefix="meshcore-discovery-")
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, MESHCORE_REPO, tmpdir,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return tmpdir


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _envs, _env_names
    tmpdir = None
    try:
        tmpdir = await _clone_for_discovery()
        _envs = load_environments(tmpdir)
        _env_names = {e.env_name for e in _envs}
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


class RegionEntryModel(BaseModel):
    name: str
    parent: Optional[str] = None
    flood: str = "allow"

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        if not validate_region_name(v):
            raise ValueError(
                "Region name must be lowercase letters, digits, -, $, or # only (max 30 chars)"
            )
        return v

    @field_validator("flood")
    @classmethod
    def check_flood(cls, v: str) -> str:
        if v not in ("allow", "deny"):
            raise ValueError("flood must be 'allow' or 'deny'")
        return v


class BuildRequestModel(BaseModel):
    env: str
    ref: str = "main"
    prs: list[int] = []
    advert_name: str = ""
    admin_password: str = ""
    advert_lat: Optional[float] = None
    advert_lon: Optional[float] = None
    wifi_ssid: str = ""
    wifi_pwd: str = ""
    regions: list[RegionEntryModel] = []

    @field_validator("advert_lat")
    @classmethod
    def check_lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not -90 <= v <= 90:
            raise ValueError("lat must be in [-90, 90]")
        return v

    @field_validator("advert_lon")
    @classmethod
    def check_lon(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not -180 <= v <= 180:
            raise ValueError("lon must be in [-180, 180]")
        return v

    @field_validator("ref")
    @classmethod
    def check_ref(cls, v: str) -> str:
        if not validate_ref(v):
            raise ValueError("Invalid git ref")
        return v

    @field_validator("prs")
    @classmethod
    def check_prs(cls, v: list[int]) -> list[int]:
        if len(v) > 10:
            raise ValueError("Too many PRs")
        if any(n <= 0 for n in v):
            raise ValueError("PR numbers must be positive integers")
        return list(dict.fromkeys(v))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/api/environments")
def get_environments():
    grouped = group_by_board(_envs)
    return [
        {
            "board": board_variant,
            "envs": [
                {"env_name": e.env_name, "firmware_type": e.firmware_type, "platform": e.platform}
                for e in envs
            ],
        }
        for board_variant, envs in grouped.items()
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

    _repeater_tag_re = re.compile(r"^repeater-(v\d+\.\d+.*)$")

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
        m = _repeater_tag_re.match(raw_tag)
        if m:
            tags.append({"label": f"{m.group(1)} ({sha[:7]})", "value": raw_tag})

    def _ver_key(t: dict):
        return tuple(int(x) for x in re.findall(r"\d+", t["label"]))

    _min_version = (1, 10, 0)
    tags = [t for t in tags if _ver_key(t) >= _min_version]
    tags.sort(key=_ver_key, reverse=True)
    _versions_cache = tags[:20] + branches
    _versions_cache_time = time.monotonic()
    return _versions_cache


@app.post("/api/build")
async def submit_build(body: BuildRequestModel):
    if body.env not in _env_names:
        raise HTTPException(400, f"Unknown environment: {body.env!r}")
    if queue.queue_depth() >= MAX_QUEUE_SIZE:
        raise HTTPException(429, "Build queue is full, try again later")

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
        raise HTTPException(404)
    return {
        "build_id": job.id,
        "env": job.env,
        "ref": job.ref,
        "status": job.status,
        "download_url": f"/api/builds/{job.id}/download" if job.status == BuildStatus.COMPLETED else None,
        "filename": job.firmware_path.name if job.firmware_path else None,
    }


@app.delete("/api/builds/{build_id}")
async def cancel_build(build_id: str):
    job = queue.get(build_id)
    if not job:
        raise HTTPException(404)
    if job.status not in (BuildStatus.PENDING, BuildStatus.RUNNING):
        raise HTTPException(409, "Build is not cancellable")
    queue.cancel(build_id)
    return {"build_id": build_id}


@app.get("/api/builds/{build_id}/logs")
async def stream_logs(build_id: str):
    job = queue.get(build_id)
    if not job:
        raise HTTPException(404)

    async def generate():
        for line in list(job.log_lines):
            yield f"data: {json.dumps(line)}\n\n"

        if job.status in (BuildStatus.COMPLETED, BuildStatus.FAILED):
            yield "data: [DONE]\n\n"
            return

        sub = queue.subscribe(build_id)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(sub.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if line is None:
                    yield "data: [DONE]\n\n"
                    return
                yield f"data: {json.dumps(line)}\n\n"
        finally:
            queue.unsubscribe(build_id, sub)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/builds/{build_id}/download")
def download_firmware(build_id: str):
    job = queue.get(build_id)
    if not job or job.status != BuildStatus.COMPLETED or not job.firmware_path:
        raise HTTPException(404)
    path = Path(job.firmware_path)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
    )


# ---------------------------------------------------------------------------
# Static files (must come last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
