from __future__ import annotations

import logging
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import BASE_DIR
from database.database import Base, engine
from database.repository import (
    get_alerts,
    get_all_detections,
    get_dashboard_stats,
    get_latest_detection,
)
from services.camera_service import generate_frames
from services.location_service import get_location_name
from services.yolo_service import process_image, process_video


# ------------------------------------------------------------
# APPLICATION
# ------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("overwatch")

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="OverWatch AI Backend",
    version="5.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------
# DIRECTORIES
# ------------------------------------------------------------

UPLOAD_DIR = Path(BASE_DIR) / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"

for directory in (UPLOAD_DIR, IMAGE_DIR, VIDEO_DIR, SNAPSHOT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOAD_DIR)),
    name="uploads",
)


# ------------------------------------------------------------
# SHARED STATE
# ------------------------------------------------------------

FALLBACK_LOCATION = "Block K, 2 Aubrey Matlala Street, Soshanguve, Pretoria"

CURRENT_LOCATION: dict[str, Any] = {
    "address": FALLBACK_LOCATION,
    "latitude": None,
    "longitude": None,
    "source": "fallback",
}



JOBS: dict[str, dict[str, Any]] = {}
jobs_lock = Lock()

# Run one uploaded-media inference job at a time. This prevents
# image and video jobs from loading/using the same YOLO models
# simultaneously and avoids occupying multiple API worker threads.
# Live-camera inference is handled separately by camera_service.
inference_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="overwatch-inference",
)


def create_job(job_id: str, data: dict[str, Any]) -> None:
    with jobs_lock:
        JOBS[job_id] = data


def update_job(job_id: str, **fields: Any) -> None:
    with jobs_lock:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def save_uploaded_file(source, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def run_image_job(job_id: str, image_path: Path, location: str) -> None:
    update_job(job_id, status="processing")

    try:
        result = process_image(str(image_path), location)

        update_job(
            job_id,
            status="completed",
            result={
                "annotated_image": result.get("annotated_image"),
                "vehicles": result.get("vehicles", []),
                "stats": result.get("stats", {}),
            },
        )
    except Exception as exc:
        logger.exception("Image processing failed (job %s)", job_id)
        update_job(job_id, status="failed", error=str(exc))


def run_video_job(
    job_id: str,
    input_path: Path,
    processed_path: Path,
    location: str,
) -> None:
    update_job(job_id, status="processing")

    try:
        result = process_video(
            str(input_path),
            str(processed_path),
            location,
        )

        update_job(
            job_id,
            status="completed",
            result={
                "processed_video": result.get("processed_video"),
                "vehicles": result.get("vehicles", []),
                "stats": result.get("stats", {}),
            },
        )
    except Exception as exc:
        logger.exception("Video processing failed (job %s)", job_id)
        update_job(job_id, status="failed", error=str(exc))


# ------------------------------------------------------------
# HEALTH
# ------------------------------------------------------------

@app.get("/")
def root():
    return {
        "status": "Running",
        "application": "OverWatch AI",
        "vehicle_model": "YOLO11n",
        "specialised_model": "YOLO26n",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


# ------------------------------------------------------------
# LOCATION
# ------------------------------------------------------------

@app.post("/location")
def update_location(location: dict):
    try:
        latitude = float(location.get("latitude"))
        longitude = float(location.get("longitude"))

        if not -90 <= latitude <= 90:
            raise HTTPException(status_code=400, detail="Invalid latitude.")

        if not -180 <= longitude <= 180:
            raise HTTPException(status_code=400, detail="Invalid longitude.")

        address = get_location_name(latitude, longitude)

        CURRENT_LOCATION.update(
            {
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

        logger.info(
            "GPS location updated: %s (%s, %s)",
            address,
            latitude,
            longitude,
        )
        return CURRENT_LOCATION.copy()

    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Valid latitude and longitude are required.",
        )


@app.get("/location")
def current_location():
    return CURRENT_LOCATION.copy()


# ------------------------------------------------------------
# DASHBOARD DATA
# ------------------------------------------------------------

@app.get("/dashboard")
def dashboard():
    return jsonable_encoder(get_dashboard_stats())


@app.get("/dashboard/latest")
def latest():
    return jsonable_encoder(get_latest_detection())


@app.get("/detections")
def detections():
    return jsonable_encoder(get_all_detections())


@app.get("/alerts")
def alerts():
    return jsonable_encoder(get_alerts())


# ------------------------------------------------------------
# IMAGE UPLOAD
# ------------------------------------------------------------

@app.post("/upload/image", status_code=202)
async def upload_image(
    file: UploadFile = File(...),
    location: str = Form("Polokwane Weighbridge"),
):
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png"}:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, JPEG and PNG images are supported.",
        )

    filename = f"{uuid.uuid4().hex}{extension}"
    image_path = IMAGE_DIR / filename

    try:
        # UploadFile is spooled by Starlette; read/write in chunks so
        # large uploads do not need to be loaded into memory at once.
        await file.seek(0)
        with image_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                output.write(chunk)
    except Exception:
        logger.exception("Image upload failed")
        raise HTTPException(
            status_code=500,
            detail="Unable to save uploaded image.",
        )
    finally:
        await file.close()

    job_id = uuid.uuid4().hex
    preview_url = f"/uploads/images/{filename}"

    create_job(
        job_id,
        {
            "status": "queued",
            "type": "image",
            "preview_image": preview_url,
            "result": None,
            "error": None,
        },
    )

    inference_executor.submit(
        run_image_job,
        job_id,
        image_path,
        location,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "preview_image": preview_url,
        "message": "Image uploaded. Detection queued.",
    }


# ------------------------------------------------------------
# VIDEO UPLOAD
# ------------------------------------------------------------

@app.post("/upload/video", status_code=202)
async def upload_video(
    file: UploadFile = File(...),
    location: str = Form("Polokwane Weighbridge"),
):
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".mp4", ".mov", ".avi", ".mkv"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format.",
        )

    filename = f"{uuid.uuid4().hex}{extension}"
    input_path = VIDEO_DIR / filename
    processed_path = VIDEO_DIR / f"processed_{filename}"

    try:
        with input_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                output.write(chunk)
    except Exception:
        logger.exception("Video upload failed")
        raise HTTPException(
            status_code=500,
            detail="Unable to save uploaded video.",
        )
    finally:
        await file.close()

    job_id = uuid.uuid4().hex
    preview_url = f"/uploads/videos/{filename}"

    create_job(
        job_id,
        {
            "status": "queued",
            "type": "video",
            "preview_video": preview_url,
            "result": None,
            "error": None,
        },
    )

    inference_executor.submit(
        run_video_job,
        job_id,
        input_path,
        processed_path,
        location,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "preview_video": preview_url,
        "message": "Video uploaded. Processing queued.",
    }


# ------------------------------------------------------------
# JOB STATUS
# ------------------------------------------------------------

@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    with jobs_lock:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Processing job not found.",
            )
        response = {"job_id": job_id, **job.copy()}

    return jsonable_encoder(response)


# ------------------------------------------------------------
# LIVE CAMERA STREAM
# ------------------------------------------------------------

@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.on_event("shutdown")
def shutdown_inference_executor():
    inference_executor.shutdown(
        wait=False,
        cancel_futures=True
    )
