from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from services.camera_service import generate_frames
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from threading import Lock
import logging
from services.location_service import get_location_name
from pathlib import Path
import shutil
import uuid

from database.database import Base, engine
from database.repository import (
    get_dashboard_stats,
    get_latest_detection,
    get_all_detections,
    get_alerts,
)

from services.yolo_service import process_image, process_video
from config import BASE_DIR

# ----------------------------------------------------

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="OverWatch AI Backend",
    version="5.0",
)

# ----------------------------------------------------

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

# ----------------------------------------------------

UPLOAD_DIR = BASE_DIR / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOAD_DIR)),
    name="uploads",
)

CURRENT_LOCATION = {
    "address": None,
    "latitude": None,
    "longitude": None,
}
# ====================================================
# BACKGROUND PROCESSING JOBS
# ====================================================

logger = logging.getLogger(__name__)

JOBS = {}

jobs_lock = Lock()

# Prevent multiple heavy YOLO jobs from competing
# for the same models/GPU simultaneously.
processing_lock = Lock()


def update_job(job_id, **fields):
    with jobs_lock:
        JOBS[job_id].update(fields)


def save_uploaded_file(source, destination):
    with open(destination, "wb") as buffer:
        shutil.copyfileobj(
            source,
            buffer,
            length=1024 * 1024
        )


def save_vehicle_records(vehicles, unique_tracks=False):
    saved_tracks = set()

    for vehicle in vehicles:
        tracker_id = vehicle.get("tracker_id")

        if unique_tracks and tracker_id in saved_tracks:
            continue

        saved_tracks.add(tracker_id)

        vehicle_location = str(
            vehicle.get("location") or ""
        ).strip()

        placeholders = {
            "",
            "current device location",
            "polokwane weighbridge",
            "waiting for gps location",
        }

        if vehicle_location.lower() in placeholders:
            vehicle_location = CURRENT_LOCATION["address"]



def process_image_background(job_id, image_path, location):

    update_job(job_id, status="processing")

    try:
       with processing_lock:
        result = process_image(image_path, location)
        vehicles = result.get("vehicles", [])

        update_job(
            job_id,
            status="completed",
            result={
                "annotated_image": result["annotated_image"],
                "vehicles": vehicles,
                "stats": result["stats"],
            },
        )

    except Exception as exc:
        logger.exception("Image processing failed: %s", job_id)

        update_job(
            job_id,
            status="failed",
            error=str(exc),
        )


def process_video_background(
    job_id,
    input_path,
    processed_path,
    location,
):

    update_job(job_id, status="processing")

    try:
       with processing_lock:
            result = process_video(
                input_path,
                processed_path,
                location,
            )

            vehicles = result.get("vehicles", [])

            update_job(
                job_id,
                status="completed",
                result={
                    "processed_video": result["processed_video"],
                    "vehicles": vehicles,
                    "stats": result["stats"],
                },
            )

    except Exception as exc:
        logger.exception("Video processing failed: %s", job_id)

        update_job(
            job_id,
            status="failed",
            error=str(exc),
        )

# ----------------------------------------------------


@app.get("/")
def root():
    return {
        "status": "Running",
        "model": "YOLOv11",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}

# ----------------------------------------------------



@app.post("/location")
def update_location(location: dict):
    try:
        latitude = float(location.get("latitude"))
        longitude = float(location.get("longitude"))

        if not (-90 <= latitude <= 90):
            raise HTTPException(
                status_code=400,
                detail="Invalid latitude."
            )

        if not (-180 <= longitude <= 180):
            raise HTTPException(
                status_code=400,
                detail="Invalid longitude."
            )

        # Convert GPS coordinates to a real place name.
        address = get_location_name(latitude, longitude)

        CURRENT_LOCATION.update({
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
        })

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
            detail="Valid latitude and longitude are required."
        )

# ----------------------------------------------------


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

# ====================================================
# IMAGE UPLOAD
# ====================================================


@app.post("/upload/image", status_code=202)
async def upload_image(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    location: str = Form("Polokwane Weighbridge"),
):
    allowed_extensions = {".jpg", ".jpeg", ".png"}

    extension = Path(file.filename or "").suffix.lower()

    if extension not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, JPEG and PNG images are supported."
        )

    filename = f"{uuid.uuid4().hex}{extension}"
    image_path = IMAGE_DIR / filename

    try:
        await run_in_threadpool(
            save_uploaded_file,
            file.file,
            image_path
        )
    except Exception:
        logger.exception("Image upload failed")
        raise HTTPException(
            status_code=500,
            detail="Unable to save uploaded image."
        )
    finally:
        await file.close()

    job_id = uuid.uuid4().hex

    preview_url = f"/uploads/images/{filename}"

    update_data = {
        "status": "queued",
        "type": "image",
        "preview_image": preview_url,
        "result": None,
        "error": None,
    }

    with jobs_lock:
        JOBS[job_id] = update_data

    background_tasks.add_task(
        process_image_background,
        job_id,
        image_path,
        location,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "preview_image": preview_url,
        "message": "Image uploaded. Detection queued."
    }



# ====================================================
# FAST VIDEO UPLOAD
# ====================================================

@app.post("/upload/video", status_code=202)
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    location: str = Form("Polokwane Weighbridge"),
):

    extension = Path(file.filename or "").suffix.lower()

    allowed_extensions = {".mp4", ".mov", ".avi", ".mkv"}

    if extension not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format."
        )

    filename = f"{uuid.uuid4().hex}{extension}"

    input_path = VIDEO_DIR / filename

    processed_path = VIDEO_DIR / f"processed_{filename}"

    try:
        await run_in_threadpool(
            save_uploaded_file,
            file.file,
            input_path,
        )

    except Exception:
        logger.exception("Video upload failed")
        raise HTTPException(
            status_code=500,
            detail="Unable to save uploaded video."
        )

    finally:
        await file.close()

    job_id = uuid.uuid4().hex

    preview_url = f"/uploads/videos/{filename}"

    with jobs_lock:
        JOBS[job_id] = {
            "status": "queued",
            "type": "video",
            "preview_video": preview_url,
            "result": None,
            "error": None,
        }

    background_tasks.add_task(
        process_video_background,
        job_id,
        input_path,
        processed_path,
        location,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "preview_video": preview_url,
        "message": "Video uploaded. Processing queued."
    }

# ====================================================
# PROCESSING STATUS
# ====================================================

@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):

    with jobs_lock:
        job = JOBS.get(job_id)

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Processing job not found"
            )

        return jsonable_encoder({
            "job_id": job_id,
            **job,
        })

# ====================================================
# CAMERA STREAM
# ====================================================





@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )
