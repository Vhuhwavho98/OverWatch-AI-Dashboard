

# ==========================================
# OVERWATCH AI - FASTAPI BACKEND
# ==========================================

import logging
import os
import csv
import inspect
import threading
import uuid
import re
import time

from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

# CPU thread limits before importing PyTorch
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")

import torch
from ultralytics import YOLO
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from services.ocr_service import detect_plate
from database.repository import (
    save_or_update_detection,
    update_detection_by_tracker_id,
    get_all_detections,
    get_latest_detection,
    get_dashboard_stats,
    get_alerts,
)

from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Create the FastAPI application
app = FastAPI(title="OverWatch AI")



# ============================================================
# CSV MIRROR PERSISTENCE
# Writes the same vehicle records to backend/detections.csv
# while retaining the existing SQL Server persistence.
# ============================================================

CSV_FILE = Path(__file__).resolve().parents[1] / "detections.csv"
CSV_FIELDS = [
    "id", "tracker_id", "vehicle_type", "registration_number",
    "confidence", "overload_status", "severity_score", "location",
    "snapshot", "plate_snippet", "timestamp", "latitude", "longitude",
]
_CSV_LOCK = threading.RLock()


def _csv_read_rows():
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
        with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _csv_write_rows(rows):
    temporary = CSV_FILE.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
    temporary.replace(CSV_FILE)


def _csv_upsert_vehicle(
    tracker_id,
    vehicle_type,
    registration_number,
    confidence,
    overload_status,
    severity_score,
    location,
    snapshot,
    plate_snippet=None,
    latitude=None,
    longitude=None,
):
    """Create one CSV row per recent tracker/location, otherwise update it."""
    try:
        with _CSV_LOCK:
            rows = _csv_read_rows()
            now = datetime.now(timezone(timedelta(hours=2)))
            cutoff = now - timedelta(minutes=5)
            match_index = None

            for index, row in enumerate(rows):
                try:
                    same_tracker = int(row.get("tracker_id") or -1) == int(tracker_id)
                except (TypeError, ValueError):
                    same_tracker = False

                if not same_tracker or row.get("location", "") != (location or ""):
                    continue

                try:
                    recorded = datetime.fromisoformat(
                        (row.get("timestamp") or "").replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except (TypeError, ValueError):
                    continue

                if recorded >= cutoff:
                    match_index = index
                    break

            registration = str(registration_number or "UNKNOWN").strip().upper()
            if registration in {"", "UNKNOWN", "N/A", "NONE"}:
                registration = None

            if match_index is None:
                next_id = max(
                    (int(row.get("id") or 0) for row in rows),
                    default=0,
                ) + 1
                record = {
                    "id": next_id,
                    "tracker_id": int(tracker_id),
                    "vehicle_type": vehicle_type or "Unknown",
                    "registration_number": registration or "UNKNOWN",
                    "confidence": float(confidence or 0.0),
                    "overload_status": overload_status or "INDETERMINATE",
                    "severity_score": float(severity_score or 0),
                    "location": location or "Unknown",
                    "snapshot": snapshot or "",
                    "plate_snippet": plate_snippet or "",
                    "timestamp": now.isoformat(timespec="seconds"),
                    "latitude": latitude if latitude is not None else "",
                    "longitude": longitude if longitude is not None else "",
                }
                rows.append(record)
            else:
                record = rows[match_index]
                record.update({
                    "vehicle_type": vehicle_type or record.get("vehicle_type", "Unknown"),
                    "confidence": float(confidence or 0.0),
                    "overload_status": overload_status or record.get("overload_status", "INDETERMINATE"),
                    "severity_score": float(severity_score or 0),
                    "location": location or record.get("location", " Block K, 2 Aubrey Matlala Street, Soshanguve Pretoria"),
                    "snapshot": snapshot or record.get("snapshot", ""),
                    "plate_snippet": plate_snippet or record.get("plate_snippet", ""),
                    "timestamp": now.isoformat(timespec="seconds"),
                })
                if registration:
                    record["registration_number"] = registration

            _csv_write_rows(rows)
            logger.info(
                "[CSV] Saved/updated tracker_id=%s at %s -> %s",
                tracker_id, CSV_FILE, CSV_FILE.exists()
            )
    except Exception:
        logger.exception("[CSV] Failed to save tracker_id=%s to %s", tracker_id, CSV_FILE)
        raise


def _csv_update_registration(tracker_id, registration_number, location, plate_snippet=None):
    """
    Update the newest matching CSV row when ANPR succeeds later.
    Do not expire the row after five minutes: a vehicle can remain tracked
    longer than that, and a delayed OCR result must still replace UNKNOWN.
    """
    registration = re.sub(r"[^A-Z0-9]", "", str(registration_number or "").upper())
    if not registration or registration in {"UNKNOWN", "OCRNOTREADABLE"}:
        return False

    with _CSV_LOCK:
        rows = _csv_read_rows()
        matches = []

        for index, row in enumerate(rows):
            try:
                same_tracker = int(row.get("tracker_id") or -1) == int(tracker_id)
            except (TypeError, ValueError):
                same_tracker = False

            if not same_tracker:
                continue

            # Prefer exact location match, but do not discard a valid ANPR
            # update solely because GPS text changed slightly between frames.
            row_location = (row.get("location") or " Block K, 2 Aubrey Matlala Street, Soshanguve Pretoria").strip()
            requested_location = (location or "").strip()
            location_match = (
                not requested_location
                or not row_location
                or row_location == requested_location
            )

            try:
                recorded = datetime.fromisoformat(
                    (row.get("timestamp") or "").replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                recorded = datetime.min

            matches.append((index, recorded, location_match))

        if not matches:
            logger.warning(
                "[CSV] No row found for ANPR update tracker_id=%s plate=%s",
                tracker_id, registration,
            )
            return False

        # Select newest matching tracker row; prefer matching location where
        # possible. This prevents an old UNKNOWN value being left untouched.
        preferred = [item for item in matches if item[2]]
        candidates = preferred or matches
        index = max(candidates, key=lambda item: item[1])[0]

        rows[index]["registration_number"] = registration
        if plate_snippet:
            rows[index]["plate_snippet"] = plate_snippet
        _csv_write_rows(rows)

        logger.info(
            "[CSV] ANPR registration updated tracker_id=%s registration=%s row_id=%s",
            tracker_id, registration, rows[index].get("id"),
        )
        return True



def _csv_update_plate_snippet(tracker_id, plate_snippet, location=None):
    """Attach a saved plate crop to the existing tracker row, even if OCR needs review."""
    if not plate_snippet:
        return False
    with _CSV_LOCK:
        rows = _csv_read_rows()
        matches = []
        for index, row in enumerate(rows):
            try:
                same_tracker = int(row.get("tracker_id") or -1) == int(tracker_id)
            except (TypeError, ValueError):
                same_tracker = False
            if not same_tracker:
                continue
            location_match = (
                not location
                or not row.get("location")
                or row.get("location") == location
            )
            matches.append((index, location_match))
        if not matches:
            logger.warning(
                "[CSV] No row found for plate snippet tracker_id=%s", tracker_id
            )
            return False
        preferred = [item for item in matches if item[1]]
        index = (preferred or matches)[-1][0]
        rows[index]["plate_snippet"] = plate_snippet
        _csv_write_rows(rows)
        logger.info(
            "[CSV] Plate snippet linked tracker_id=%s snippet=%s",
            tracker_id, plate_snippet,
        )
        return True


from pathlib import Path

# ==========================================
# MODEL PATH CONFIGURATION
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models"

# YOLO11 vehicle detector
VEHICLE_NCNN_PATH = MODEL_DIR / "yolo11n_ncnn_model"
VEHICLE_PT_PATH = MODEL_DIR / "yolo11n.pt"

# YOLO26 specialised plate detector
SPECIAL_NCNN_PATH = MODEL_DIR / "yolo26n_ncnn_model"
SPECIAL_PT_PATH = MODEL_DIR / "yolo26n.pt"

# Custom trained model
CUSTOM_MODEL_PATH = MODEL_DIR / "best.pt"

# Dedicated number plate detector
PLATE_MODEL_PATH = MODEL_DIR / "license_plate_detector.pt"

VEHICLE_MODEL_PATH = (
    VEHICLE_NCNN_PATH if (VEHICLE_NCNN_PATH / "model.ncnn.param").is_file()
    else VEHICLE_PT_PATH
)
CARGO_PLATE_MODEL_PATH = (
    SPECIAL_NCNN_PATH if (SPECIAL_NCNN_PATH / "model.ncnn.param").is_file()
    else SPECIAL_PT_PATH
)

UPLOAD_DIR = BASE_DIR / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"
PLATE_DIR = UPLOAD_DIR / "plates"

for directory in (IMAGE_DIR, VIDEO_DIR, SNAPSHOT_DIR, PLATE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

for model_path in (VEHICLE_MODEL_PATH, CARGO_PLATE_MODEL_PATH):
    exists = (model_path / "model.ncnn.param").is_file() if model_path.is_dir() else model_path.is_file()
    if not exists:
        raise FileNotFoundError(f"Required YOLO model missing: {model_path}")

vehicle_model = YOLO(str(VEHICLE_MODEL_PATH))
cargo_plate_model = YOLO(str(CARGO_PLATE_MODEL_PATH))
# This deployment profile targets the user's Windows Ryzen 5 7530U CPU.
# Override these environment variables when deploying to the 8 GB Raspberry Pi.
DEVICE = os.getenv("OVERWATCH_DEVICE", "cpu")
try:
    torch.set_num_threads(int(os.getenv("OVERWATCH_TORCH_THREADS", "4")))
    torch.set_num_interop_threads(int(os.getenv("OVERWATCH_TORCH_INTEROP_THREADS", "2")))
except RuntimeError:
    # PyTorch may already have initialised its thread pool in some reload modes.
    logger.debug("PyTorch thread pool was already initialised")
VEHICLE_IS_NCNN = VEHICLE_MODEL_PATH.is_dir()
SPECIAL_IS_NCNN = CARGO_PLATE_MODEL_PATH.is_dir()


def _normalise_names(names):
    if isinstance(names, dict):
        return {int(k): str(v).strip().lower() for k, v in names.items()}
    return {i: str(v).strip().lower() for i, v in enumerate(names)}


VEHICLE_NAMES = _normalise_names(vehicle_model.names)
SPECIAL_NAMES = _normalise_names(cargo_plate_model.names)

CARGO_CLASS_ALIASES = {
    "cargo", "load", "luggage", "roof_load", "roof cargo",
    "cargo_load", "loaded_goods", "stacked_load",
    "tyre_load", "tire_load", "freight", "parcel", "baggage",
}
CARGO_CLASS_AVAILABLE = any(
    name in CARGO_CLASS_ALIASES
    or any(token in name for token in (
        "cargo_load", "loaded_goods", "stacked_load",
        "tyre_load", "tire_load", "roof_load",
    ))
    for name in SPECIAL_NAMES.values()
)

VEHICLE_CLASS_IDS = [1, 2, 3, 5, 7]

VEHICLE_CLASSES = {
    "bicycle",
    "motorcycle",
    "car",
    "pickup",
    "minibus",
    "midibus",
    "bus",
    "truck",
}

logger.info("YOLO11 vehicle classes: %s", VEHICLE_NAMES)
logger.info("YOLO26 cargo/plate classes: %s", SPECIAL_NAMES)
logger.info(
    "YOLO26 cargo-class capability: %s",
    "AVAILABLE" if CARGO_CLASS_AVAILABLE else "MISSING - cargo risk cannot be positively classified by class",
)
logger.info("Inference device: %s", DEVICE)
logger.info("YOLO11 backend: %s", "NCNN" if VEHICLE_IS_NCNN else "PyTorch")
logger.info("YOLO26 backend: %s", "NCNN" if SPECIAL_IS_NCNN else "PyTorch")

print("\n========== OVERWATCH AI MODELS ==========")
print("Vehicle model:", VEHICLE_MODEL_PATH)
print("Vehicle classes:", VEHICLE_NAMES)
print("Cargo/plate model:", CARGO_PLATE_MODEL_PATH)
print("Cargo/plate classes:", SPECIAL_NAMES)
print("Cargo class available:", CARGO_CLASS_AVAILABLE)
print("Vehicle class IDs:", VEHICLE_CLASS_IDS)
print("Inference device:", DEVICE)
print("Vehicle backend:", "NCNN" if VEHICLE_IS_NCNN else "PyTorch")
print("Specialised backend:", "NCNN" if SPECIAL_IS_NCNN else "PyTorch")
print("=========================================\n")


# ==========================================
# OVERWATCH AI - SPEED / ACCURACY CONFIGURATION
# ==========================================

# YOLO11 - Vehicle Detection and Tracking
YOLO_IMAGE_SIZE = int(
    os.getenv("OVERWATCH_YOLO_IMAGE_SIZE", "512")
)

YOLO_CONFIDENCE = 0.45
YOLO_IOU = 0.50
MAX_DETECTIONS = 50

# YOLO26 - Cargo and Number Plate Detection
SPECIAL_IMAGE_SIZE = 320
SPECIAL_CONFIDENCE = 0.50

# Maximum specialised detections per inference
SPECIAL_MAX_DETECTIONS = 40

# Run specialised inference every N frames per tracked vehicle.
# Reuse cached detections between inference runs.
SPECIAL_INFERENCE_INTERVAL = int(
    os.getenv("OVERWATCH_SPECIAL_INTERVAL", "8")
)

SPECIAL_CACHE_MAX_AGE = 12

# Performance monitoring
PERFORMANCE_LOG_INTERVAL = 60

# ==========================================
# DETECTION ZONE
# ==========================================

DETECTION_POLYGON = np.array(
    [[20, 90], [1260, 90], [1235, 700], [40, 700]],
    dtype=np.int32
)

REFERENCE_WIDTH, REFERENCE_HEIGHT = 1280, 720

FRAME_MARGIN_FRACTION = 0.025

# Virtual analysis line. Set OVERWATCH_DETECTION_LINE_ORIENTATION to
# "horizontal" or "vertical". The position is a percentage of frame size.
# horizontal=70 means 70% down from the top; vertical=70 means 70% from left.
DETECTION_LINE_ORIENTATION = os.getenv(
    "OVERWATCH_DETECTION_LINE_ORIENTATION", "horizontal"
).strip().lower()
if DETECTION_LINE_ORIENTATION not in {"horizontal", "vertical"}:
    logger.warning("Invalid detection-line orientation %r; using horizontal", DETECTION_LINE_ORIENTATION)
    DETECTION_LINE_ORIENTATION = "horizontal"
DETECTION_LINE_FRACTION = float(os.getenv("OVERWATCH_DETECTION_LINE_POSITION", "0.70"))
DETECTION_LINE_FRACTION = min(0.95, max(0.05, DETECTION_LINE_FRACTION))

# ==========================================
# VEHICLE TRACKING AND SAVING
# ==========================================

STABLE_FRAMES = 4
MIN_CONFIDENCE = 0.25

# ==========================================
# NUMBER PLATE OCR
# ==========================================

MAX_OCR_ATTEMPTS = 8

OCR_RETRY_INTERVAL = int(
    os.getenv("OVERWATCH_OCR_INTERVAL", "45")
)


def _polygon_for_frame(width, height):
    polygon = DETECTION_POLYGON.astype(np.float32).copy()
    polygon[:, 0] *= width / REFERENCE_WIDTH
    polygon[:, 1] *= height / REFERENCE_HEIGHT
    polygon[:, 0] = np.clip(polygon[:, 0], 0, width - 1)
    polygon[:, 1] = np.clip(polygon[:, 1], 0, height - 1)
    return polygon.astype(np.int32)


def inside_detection_zone(point, width=REFERENCE_WIDTH, height=REFERENCE_HEIGHT):
    return cv2.pointPolygonTest(
        _polygon_for_frame(width, height),
        (float(point[0]), float(point[1])),
        False,
    ) >= 0


def _visibility_reason(box, polygon, frame_w, frame_h):
    x1, y1, x2, y2 = box
    margin_x = max(5, int(frame_w * FRAME_MARGIN_FRACTION))
    margin_y = max(5, int(frame_h * FRAME_MARGIN_FRACTION))
    if (
        x1 <= margin_x or y1 <= margin_y
        or x2 >= frame_w - margin_x or y2 >= frame_h - margin_y
    ):
        return "PARTIAL VEHICLE - FRAME EDGE"

    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    if any(
        cv2.pointPolygonTest(polygon, (float(x), float(y)), False) < 0
        for x, y in corners
    ):
        return "OUTSIDE DETECTION ZONE"
    return None


def assess_cargo_risk(
    vehicle_crop,
    vehicle_type="car",
    specialised_regions=None,
    vehicle_box=None,
):
    """
    Screen for visibly unsafe cargo protruding above or beyond a vehicle.

    YOLO26 must detect the trained 'cargo' class. The cargo box must also be
    spatially associated with this vehicle and extend beyond its detected
    body box. This is a visual risk indicator, not a measurement of mass.
    """
    result = {
        "cargo_risk_score": 0,
        "cargo_risk_level": "NORMAL",
        "cargo_indicators": [],
        "cargo_assessment_method": "cargo_detection_plus_vehicle_box_geometry",
        "cargo_model_status": "NO_CARGO_DETECTED",
        "cargo_candidate_count": 0,
    }

    # Do not silently call a vehicle NORMAL if the loaded specialised model
    # has no cargo/load class in its taxonomy. That would be a false-safe
    # result: a standard COCO detector cannot positively identify arbitrary
    # cargo such as tarpaulin-covered freight.
    if not CARGO_CLASS_AVAILABLE:
        result.update(
            cargo_risk_level="UNKNOWN",
            cargo_model_status="MODEL_HAS_NO_CARGO_CLASS",
            cargo_assessment_method="UNSUPPORTED_MODEL_CLASS_TAXONOMY",
        )
        return result

    if (vehicle_crop is None or not isinstance(vehicle_crop, np.ndarray)
            or vehicle_crop.size == 0 or vehicle_crop.ndim < 2):
        result.update(cargo_risk_level="UNKNOWN",
                      cargo_model_status="INVALID_VEHICLE_CROP")
        return result

    crop_h, crop_w = vehicle_crop.shape[:2]
    if crop_h < 30 or crop_w < 30 or not vehicle_box:
        result.update(cargo_risk_level="UNKNOWN",
                      cargo_model_status="INVALID_VEHICLE_GEOMETRY")
        return result

    vx1, vy1, vx2, vy2 = map(float, vehicle_box)
    vehicle_w = max(1.0, vx2 - vx1)
    vehicle_h = max(1.0, vy2 - vy1)

    # A cargo box can sit partly ABOVE the vehicle box, so do not require
    # its centre to be inside the vehicle rectangle.
    candidates = []
    for region in specialised_regions or []:
        name = str(region.get("class_name", "")).strip().lower()
        confidence = float(region.get("confidence", 0.0))
        box = region.get("box")
        cargo_class = (
            name in CARGO_CLASS_ALIASES
            or any(token in name for token in (
                "cargo_load", "loaded_goods", "stacked_load",
                "tyre_load", "tire_load", "roof_load",
            ))
        )
        if not cargo_class:
            continue
        if confidence < 0.45 or not box or len(box) != 4:
            continue

        cx1, cy1, cx2, cy2 = map(float, box)
        if cx2 <= cx1 or cy2 <= cy1:
            continue

        # Require meaningful horizontal overlap with the vehicle body.
        overlap_w = max(0.0, min(vx2, cx2) - max(vx1, cx1))
        cargo_w = max(1.0, cx2 - cx1)
        overlap_ratio = overlap_w / cargo_w
        horizontal_near = (
            cx2 >= vx1 - 0.12 * vehicle_w
            and cx1 <= vx2 + 0.12 * vehicle_w
        )
        if not horizontal_near or overlap_ratio < 0.25:
            continue

        # Cargo above the roof must touch/overlap the top region of the
        # vehicle box. This rejects signs, people and background objects.
        vertical_gap = vy1 - cy2
        roof_attached = (
            cy1 < vy1
            and vertical_gap <= 0.12 * vehicle_h
            and vertical_gap >= -0.10 * vehicle_h
        )

        # Side protrusion: cargo overlaps the vehicle vertically but extends
        # clearly past either side of the vehicle body.
        vertical_overlap = max(0.0, min(vy2, cy2) - max(vy1, cy1))
        side_extension = (
            vertical_overlap >= 0.20 * vehicle_h
            and (
                cx1 < vx1 - 0.08 * vehicle_w
                or cx2 > vx2 + 0.08 * vehicle_w
            )
        )

        if roof_attached or side_extension:
            candidates.append({
                "confidence": confidence,
                "roof_protrusion": roof_attached,
                "side_protrusion": side_extension,
                "box": [int(cx1), int(cy1), int(cx2), int(cy2)],
            })

    result["cargo_candidate_count"] = len(candidates)
    if candidates:
        result["cargo_risk_score"] = min(
            100, int(round(max(c["confidence"] for c in candidates) * 100))
        )
        result["cargo_risk_level"] = "WARNING"
        result["cargo_model_status"] = "VISIBLE_CARGO_PROTRUSION"
        if any(c["roof_protrusion"] for c in candidates):
            result["cargo_indicators"].append("CARGO_PROTRUDING_ABOVE_ROOF")
        if any(c["side_protrusion"] for c in candidates):
            result["cargo_indicators"].append("CARGO_PROTRUDING_BEYOND_SIDE")
        result["cargo_regions"] = candidates

    return result


def classify_overload(vehicle_type, width, height, cargo_assessment=None):
    """Classify only what the cargo model can support; avoid false-safe output."""
    if not cargo_assessment:
        return "REVIEW_REQUIRED"
    level = str(cargo_assessment.get("cargo_risk_level", "UNKNOWN")).upper()
    if level == "WARNING":
        return "WARNING"
    if level in {"UNKNOWN", "INDETERMINATE"}:
        return "REVIEW_REQUIRED"
    return "NORMAL"


def _save_plate_candidate_crop(frame, plate_box, tracker_id):
    """Save an enlarged, padded crop around a YOLO26 plate candidate.

    This guarantees a visible review snippet even when OCR cannot read the
    registration. Coordinates are in full-frame pixels.
    """
    if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
        return None
    if not plate_box or len(plate_box) != 4:
        return None

    frame_h, frame_w = frame.shape[:2]
    try:
        x1, y1, x2, y2 = [int(round(float(v))) for v in plate_box]
    except (TypeError, ValueError):
        return None

    # Reject invalid/tiny detector proposals; expand enough to retain the
    # plate border and nearby bodywork without turning this into a vehicle crop.
    box_w, box_h = x2 - x1, y2 - y1
    if box_w < 3 or box_h < 3:
        return None

    pad_x = max(8, int(box_w * 0.35))
    pad_y = max(6, int(box_h * 0.55))
    left = max(0, x1 - pad_x)
    top = max(0, y1 - pad_y)
    right = min(frame_w, x2 + pad_x)
    bottom = min(frame_h, y2 + pad_y)
    if right <= left or bottom <= top:
        return None

    plate_crop = frame[top:bottom, left:right].copy()
    if plate_crop.size == 0:
        return None

    # Enlarge small distant plates for the dashboard review panel.
    crop_h, crop_w = plate_crop.shape[:2]
    target_w = min(900, max(crop_w, 420))
    scale = target_w / max(1, crop_w)
    if scale > 1.0:
        plate_crop = cv2.resize(
            plate_crop, (target_w, max(1, int(crop_h * scale))),
            interpolation=cv2.INTER_CUBIC,
        )

    PLATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_track = re.sub(r"[^A-Za-z0-9_-]", "_", str(tracker_id or "untracked"))
    filename = f"plate_{safe_track}_{uuid.uuid4().hex[:12]}.jpg"
    destination = PLATE_DIR / filename
    if cv2.imwrite(str(destination), plate_crop, [cv2.IMWRITE_JPEG_QUALITY, 96]):
        url = f"/uploads/plates/{filename}"
        logger.info("[ANPR] saved detector plate crop tracker_id=%s url=%s", tracker_id, url)
        return url

    logger.error("[ANPR] failed to save detector plate crop: %s", destination)
    return None


def _read_plate(
    crop,
    plate_bbox=None,
    plate_detection_confidence=0.0,
    tracker_id=None,
):
    """Run ANPR and save the localized plate crop against its vehicle track."""
    if crop is None or not isinstance(crop, np.ndarray) or crop.size == 0:
        return "UNKNOWN", 0.0, None, "OCR_FAILED", None

    try:
        # Pass the YOLO26 plate candidate when the installed OCR service
        # supports it. The OCR service also scans the entire vehicle crop at
        # multiple sizes, so front and rear plates are both considered.
        # Signature filtering keeps compatibility with older OCR versions.
        try:
            accepted_parameters = inspect.signature(detect_plate).parameters
        except (TypeError, ValueError):
            accepted_parameters = {}

        ocr_kwargs = {"debug": True}
        if "plate_bbox" in accepted_parameters:
            ocr_kwargs["plate_bbox"] = plate_bbox
        if "plate_detection_confidence" in accepted_parameters:
            ocr_kwargs["plate_detection_confidence"] = plate_detection_confidence
        result = detect_plate(crop, **ocr_kwargs)
        if not isinstance(result, dict) or not result:
            return "UNKNOWN", 0.0, None, "NO_PLATE_BOX", None

        raw_registration = str(
            result.get("registration") or result.get("text") or ""
        ).strip().upper()
        registration = re.sub(r"[^A-Z0-9]", "", raw_registration)
        ocr_confidence = float(result.get("ocr_confidence") or 0.0)
        bbox = result.get("bbox")
        status = str(result.get("status") or "OCR_FAILED").upper()
        detector_confidence = float(result.get("confidence") or 0.0)

        logger.info(
            "[ANPR] tracker_id=%s detector_confidence=%.2f%% "
            "ocr_confidence=%.2f%% raw=%r status=%s bbox=%s source=%s",
            tracker_id, detector_confidence, ocr_confidence,
            raw_registration, status, bbox,
            result.get("plate_box_source", "dedicated_plate_model"),
        )

        letters = sum(ch.isalpha() for ch in registration)
        digits = sum(ch.isdigit() for ch in registration)
        valid_candidate = (
            4 <= len(registration) <= 10
            and letters >= 1
            and digits >= 1
            and registration not in {"OCRNOTREADABLE", "UNKNOWN"}
        )
        # Save the localized colour crop even when OCR is uncertain. This
        # gives the operator a close-up review image instead of losing a
        # useful plate candidate merely because recognition did not pass.
        plate_crop = result.get("plate_crop")
        snippet_url = None
        if isinstance(plate_crop, np.ndarray) and plate_crop.size:
            PLATE_DIR.mkdir(parents=True, exist_ok=True)
            safe_track = re.sub(r"[^A-Za-z0-9_-]", "_", str(tracker_id or "untracked"))
            filename = f"plate_{safe_track}_{uuid.uuid4().hex[:12]}.jpg"
            destination = PLATE_DIR / filename
            if cv2.imwrite(
                str(destination), plate_crop,
                [cv2.IMWRITE_JPEG_QUALITY, 96],
            ):
                snippet_url = f"/uploads/plates/{filename}"
                logger.info(
                    "[ANPR] saved plate snippet tracker_id=%s url=%s",
                    tracker_id, snippet_url,
                )
            else:
                logger.error("[ANPR] failed to write plate snippet: %s", destination)

        if not valid_candidate or status != "READ" or ocr_confidence < 20.0:
            logger.info(
                "[ANPR] plate not confirmed tracker_id=%s raw=%r "
                "confidence=%.2f status=%s snippet=%s",
                tracker_id, raw_registration, ocr_confidence, status, snippet_url,
            )
            return (
                "UNKNOWN", ocr_confidence, bbox,
                "NO_PLATE_BOX" if status == "NO_PLATE_BOX" else "NEEDS_REVIEW",
                snippet_url,
            )

        logger.info(
            "[ANPR] accepted tracker_id=%s registration=%s confidence=%.2f",
            tracker_id, registration, ocr_confidence,
        )
        return registration, ocr_confidence, bbox, "READ", snippet_url

    except Exception:
        logger.exception("[ANPR] Number plate OCR failed tracker_id=%s", tracker_id)
        return "UNKNOWN", 0.0, None, "OCR_EXCEPTION", None


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return overlap / max(1, area_a + area_b - overlap)


def _canonical_vehicle_name(raw_name):
    """Normalize model labels while retaining the model's own class mapping."""
    name = re.sub(r"[\s_-]+", " ", str(raw_name or "").strip().lower())
    aliases = {
        "motorbike": "motorcycle",
        "motor cycle": "motorcycle",
        "pickup truck": "pickup",
        "pick up": "pickup",
        "delivery van": "van",
        "mini bus": "minibus",
        "minibus": "minibus",
    }
    return aliases.get(name, name)


def _collect_candidates(results, frame_w, frame_h):
    """Extract vehicle boxes robustly from YOLO results and log rejected labels."""
    candidates = []
    raw_seen = []
    for result in results or []:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            try:
                class_id = int(box.cls[0].item())
                raw_name = VEHICLE_NAMES.get(class_id, str(class_id))
                vehicle_type = _canonical_vehicle_name(raw_name)
                confidence = float(box.conf[0].item())
                coords = box.xyxy[0].tolist()
                x1, y1, x2, y2 = [int(round(float(v))) for v in coords]
            except (AttributeError, IndexError, TypeError, ValueError, RuntimeError):
                logger.debug("Skipping malformed YOLO detection box", exc_info=True)
                continue

            raw_seen.append((str(raw_name), round(confidence, 3)))
            if vehicle_type not in VEHICLE_CLASSES:
                continue

            x1 = max(0, min(x1, frame_w - 1))
            y1 = max(0, min(y1, frame_h - 1))
            x2 = max(0, min(x2, frame_w))
            y2 = max(0, min(y2, frame_h))
            if x2 - x1 < 8 or y2 - y1 < 8:
                continue

            track_id = None
            try:
                if box.id is not None:
                    track_id = int(box.id.item())
            except (AttributeError, TypeError, ValueError, RuntimeError):
                track_id = None
            candidates.append((confidence, vehicle_type, (x1, y1, x2, y2), track_id))

    if raw_seen and not candidates:
        logger.warning(
            "[YOLO11] Raw detections were produced but none matched vehicle classes. "
            "Configured names=%s raw=%s",
            VEHICLE_NAMES, raw_seen[:30],
        )
    elif candidates:
        logger.debug("[YOLO11] Accepted vehicle candidates=%d raw=%s", len(candidates), raw_seen[:20])

    candidates.sort(key=lambda item: item[0], reverse=True)
    accepted = []
    for candidate in candidates:
        _, vehicle_type, box, _ = candidate
        if any(
            vehicle_type == prev[1] and _iou(box, prev[2]) > 0.60
            for prev in accepted
        ):
            continue
        accepted.append(candidate)
    return accepted


def _detect_specialised_regions_for_vehicles(frame, candidates, plate_cache=None, frame_index=0):
    """Run YOLO26n on vehicle crops periodically and reuse per-track results.

    Cached boxes are stored relative to each vehicle box, then remapped to the
    current vehicle position. This keeps annotations aligned while a vehicle
    moves between specialised-model inference frames.
    """
    if not candidates:
        return []

    if plate_cache is None:
        plate_cache = {}

    pending_crops = []
    pending_meta = []
    regions_by_record = {}

    for confidence, vehicle_type, vehicle_box, track_id in candidates:
        x1, y1, x2, y2 = vehicle_box
        record_id = int(track_id) if track_id is not None else int(
            (x1 * 73856093 + y1 * 19349663 + x2 * 83492791 + y2 * 2654435761)
            % 1_000_000_000
        )
        entry = plate_cache.setdefault(record_id, {})
        entry.setdefault("registration", "UNKNOWN")
        entry.setdefault("ocr_confidence", 0.0)
        entry.setdefault("attempts", 0)
        entry.setdefault("last_attempt", -10_000)
        entry.setdefault("bbox", None)
        entry.setdefault("stable", 0)
        entry.setdefault("last_seen", -10_000)
        entry.setdefault("ocr_status", "NOT_ATTEMPTED")
        entry.setdefault("plate_snippet", None)
        cached = entry.get("specialised_cache")
        last_frame = int(entry.get("specialised_last_frame", -10_000))
        needs_refresh = (
            cached is None
            or frame_index - last_frame >= SPECIAL_INFERENCE_INTERVAL
            or frame_index - last_frame > SPECIAL_CACHE_MAX_AGE
        )

        if needs_refresh:
            # Include substantial context above and beside the vehicle so
            # an oversized load is not clipped before YOLO26 sees it.
            crop, offset_x, offset_y = _padded_vehicle_crop(
                frame, vehicle_box, horizontal=0.22, vertical=0.48
            )
            if crop is not None and crop.size:
                pending_crops.append(crop)
                pending_meta.append((record_id, vehicle_box, x1 - offset_x, y1 - offset_y))
        else:
            regions_by_record[record_id] = _remap_cached_regions(cached, vehicle_box)

    if pending_crops:
        try:
            kwargs = {
                "source": pending_crops,
                "imgsz": SPECIAL_IMAGE_SIZE,
                "conf": SPECIAL_CONFIDENCE,
                "iou": YOLO_IOU,
                "max_det": SPECIAL_MAX_DETECTIONS,
                "verbose": False,
            }
            if not SPECIAL_IS_NCNN:
                kwargs["device"] = DEVICE
            results = cargo_plate_model.predict(**kwargs)
        except Exception:
            logger.exception("[YOLO26] Batched vehicle-crop inference failed")
            results = []

        frame_h, frame_w = frame.shape[:2]
        for result, (record_id, vehicle_box, crop_left, crop_top) in zip(results or [], pending_meta):
            x1, y1, x2, y2 = vehicle_box
            vehicle_w, vehicle_h = max(1, x2 - x1), max(1, y2 - y1)
            absolute_regions = []
            if result.boxes is not None:
                for box in result.boxes:
                    class_id = int(box.cls[0].item())
                    cx1, cy1, cx2, cy2 = [int(v) for v in box.xyxy[0].tolist()]
                    ax1 = max(0, min(frame_w, cx1 + crop_left))
                    ay1 = max(0, min(frame_h, cy1 + crop_top))
                    ax2 = max(0, min(frame_w, cx2 + crop_left))
                    ay2 = max(0, min(frame_h, cy2 + crop_top))
                    if ax2 <= ax1 or ay2 <= ay1:
                        continue
                    absolute_regions.append({
                        "class_id": class_id,
                        "class_name": SPECIAL_NAMES.get(class_id, f"class_{class_id}"),
                        "confidence": float(box.conf[0].item()),
                        "box": (ax1, ay1, ax2, ay2),
                    })

            # Cache normalized coordinates relative to the vehicle, not the frame.
            normalized = []
            for region in absolute_regions:
                rx1, ry1, rx2, ry2 = region["box"]
                normalized.append({
                    "class_id": region["class_id"],
                    "class_name": region["class_name"],
                    "confidence": region["confidence"],
                    "relative_box": (
                        (rx1 - x1) / vehicle_w,
                        (ry1 - y1) / vehicle_h,
                        (rx2 - x1) / vehicle_w,
                        (ry2 - y1) / vehicle_h,
                    ),
                })
            entry = plate_cache.setdefault(record_id, {})
            entry["specialised_cache"] = normalized
            entry["specialised_last_frame"] = frame_index
            regions_by_record[record_id] = absolute_regions

    # Return a flat list, preserving the existing _regions_for_vehicle interface.
    return [region for group in regions_by_record.values() for region in group]


def _remap_cached_regions(cached_regions, vehicle_box):
    x1, y1, x2, y2 = vehicle_box
    width, height = max(1, x2 - x1), max(1, y2 - y1)
    remapped = []
    for item in cached_regions or []:
        rx1, ry1, rx2, ry2 = item["relative_box"]
        box = (
            int(x1 + rx1 * width), int(y1 + ry1 * height),
            int(x1 + rx2 * width), int(y1 + ry2 * height),
        )
        remapped.append({
            "class_id": item["class_id"],
            "class_name": item["class_name"],
            "confidence": item["confidence"],
            "box": box,
        })
    return remapped


def _regions_for_vehicle(regions, vehicle_box):
    x1, y1, x2, y2 = vehicle_box
    pad_x = max(5, int((x2 - x1) * 0.15))
    pad_y = max(5, int((y2 - y1) * 0.15))
    left, top = x1 - pad_x, y1 - pad_y
    right, bottom = x2 + pad_x, y2 + pad_y
    matched = []
    for region in regions:
        rx1, ry1, rx2, ry2 = region["box"]
        ix1, iy1 = max(left, rx1), max(top, ry1)
        ix2, iy2 = min(right, rx2), min(bottom, ry2)
        if ix2 > ix1 and iy2 > iy1:
            matched.append(region)
    return matched


def _label(frame, line, x, y, color, scale=0.48):
    position = (max(2, x), max(19, y))
    cv2.putText(
        frame, str(line), position, cv2.FONT_HERSHEY_SIMPLEX,
        scale, (0, 0, 0), 3, cv2.LINE_AA
    )
    cv2.putText(
        frame, str(line), position, cv2.FONT_HERSHEY_SIMPLEX,
        scale, color, 1, cv2.LINE_AA
    )


def _padded_vehicle_crop(frame, box, horizontal=0.10, vertical=0.12):
    """Add context around a vehicle so a plate near a box edge is not clipped."""
    x1, y1, x2, y2 = box
    frame_h, frame_w = frame.shape[:2]
    pad_x = max(4, int((x2 - x1) * horizontal))
    pad_y = max(4, int((y2 - y1) * vertical))
    left = max(0, x1 - pad_x)
    top = max(0, y1 - pad_y)
    right = min(frame_w, x2 + pad_x)
    bottom = min(frame_h, y2 + pad_y)
    crop = frame[top:bottom, left:right]
    # Offsets from the padded crop back to the original vehicle crop.
    offset_x, offset_y = x1 - left, y1 - top
    return crop, offset_x, offset_y


def _analyse_frame(
    frame, location, saved_tracks, plate_cache, frame_index=0,
    video=False, records_by_id=None,
):
    if frame is None or frame.size == 0:
        raise ValueError("Empty or invalid image/frame")

    height, width = frame.shape[:2]
    annotated = frame.copy()
    polygon = _polygon_for_frame(width, height)
    cv2.polylines(annotated, [polygon], True, (0, 255, 180), 2)

    # Configurable virtual-camera analysis line.
    # Horizontal: percentage measured down from the top of the frame.
    # Vertical: percentage measured from the left edge of the frame.
    if DETECTION_LINE_ORIENTATION == "vertical":
        detection_line_position = int(width * DETECTION_LINE_FRACTION)
        if video:
            cv2.line(
                annotated, (detection_line_position, 0),
                (detection_line_position, height - 1),
                (0, 200, 255), 2, cv2.LINE_AA
            )
            _label(
                annotated, f"AI ANALYSIS LINE - {DETECTION_LINE_FRACTION:.0%} VERTICAL",
                max(8, detection_line_position - 180), 24,
                (0, 200, 255), scale=0.50
            )
    else:
        detection_line_position = int(height * DETECTION_LINE_FRACTION)
        if video:
            cv2.line(
                annotated, (0, detection_line_position),
                (width - 1, detection_line_position),
                (0, 200, 255), 2, cv2.LINE_AA
            )
            _label(
                annotated, f"AI ANALYSIS LINE - {DETECTION_LINE_FRACTION:.0%} HORIZONTAL",
                12, max(24, detection_line_position - 10),
                (0, 200, 255), scale=0.50
            )

    frame_started = time.perf_counter()

    # Stage 1: YOLO11n detects/tracks vehicles on every analysed frame.
    # Keeping this stage per-frame is important for ByteTrack continuity.
    vehicle_started = time.perf_counter()
    vehicle_kwargs = {
        "imgsz": YOLO_IMAGE_SIZE,
        "conf": YOLO_CONFIDENCE,
        "iou": YOLO_IOU,
        "max_det": MAX_DETECTIONS,
        "classes": VEHICLE_CLASS_IDS,
        "verbose": False,
    }
    if not VEHICLE_IS_NCNN:
        vehicle_kwargs["device"] = DEVICE

    with torch.inference_mode():
        if video:
            vehicle_results = vehicle_model.track(
                frame, persist=True, tracker="bytetrack.yaml", **vehicle_kwargs
            )
            # Do not fall back to predict() here. A second inference would
            # bypass ByteTrack and produce detections without persistent IDs.
            if not any(
                getattr(result, "boxes", None) is not None
                and len(result.boxes) > 0
                for result in (vehicle_results or [])
            ):
                logger.debug(
                    "[YOLO11] No tracked vehicle boxes on frame=%s; "
                    "continuing with the tracker state intact.",
                    frame_index,
                )
        else:
            vehicle_results = vehicle_model.predict(frame, **vehicle_kwargs)

    vehicle_ms = (time.perf_counter() - vehicle_started) * 1000.0
    detections = []
    stats = {"vehicles": 0, "warnings": 0, "overloaded": 0}
    candidates = _collect_candidates(vehicle_results, width, height)
    if not candidates:
        raw_summary = []
        for result in vehicle_results or []:
            if result.boxes is None:
                continue
            for box in result.boxes:
                class_id = int(box.cls[0].item())
                raw_summary.append({
                    "class": VEHICLE_NAMES.get(class_id, str(class_id)),
                    "confidence": round(float(box.conf[0].item()), 3),
                })
        logger.warning(
            "[YOLO11] No accepted vehicles: backend=%s conf=%.2f imgsz=%s "
            "frame=%sx%s raw_detections=%s",
            "NCNN" if VEHICLE_IS_NCNN else "PyTorch",
            YOLO_CONFIDENCE, YOLO_IMAGE_SIZE, width, height, raw_summary[:30],
        )

    # Decide which tracked vehicles have crossed the 70% line. YOLO11/ByteTrack
    # continues tracking all vehicles, but expensive YOLO26/OCR/snapshot work is
    # restricted to vehicles that have reached the trigger line.
    analysis_candidates = []
    for confidence, vehicle_type, vehicle_box, track_id in candidates:
        x1, y1, x2, y2 = vehicle_box
        record_id = int(track_id) if track_id is not None else int(
            (x1 * 73856093 + y1 * 19349663 + x2 * 83492791 + y2 * 2654435761)
            % 1_000_000_000
        )
        entry = plate_cache.setdefault(record_id, {})
        if DETECTION_LINE_ORIENTATION == "vertical":
            center_position = (x1 + x2) / 2.0
            previous_position = entry.get("previous_center_x")
            line_name = "vertical"
        else:
            center_position = (y1 + y2) / 2.0
            previous_position = entry.get("previous_center_y")
            line_name = "horizontal"

        if not video:
            entry["line_crossed"] = True
        elif not entry.get("line_crossed", False):
            # Trigger on a true crossing in either direction. If a track first
            # appears on the far side, process it immediately rather than miss it.
            crossed_now = (
                center_position >= detection_line_position
                if previous_position is None
                else (
                    previous_position < detection_line_position <= center_position
                    or previous_position > detection_line_position >= center_position
                )
            )
            if crossed_now:
                entry["line_crossed"] = True
                entry["line_crossed_frame"] = frame_index
                logger.info(
                    "[TRIGGER] tracker_id=%s crossed %s analysis line at %.0f%% frame=%s",
                    record_id, line_name, DETECTION_LINE_FRACTION * 100, frame_index
                )

        if DETECTION_LINE_ORIENTATION == "horizontal":
            entry["previous_center_x"] = center_position
        else:
            entry["previous_center_y"] = center_position

        if entry.get("line_crossed", False):
            analysis_candidates.append(
                (confidence, vehicle_type, vehicle_box, track_id)
            )
        else:
            # Lightweight visual tracking only before the trigger line.
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (255, 180, 0), 2)
            _label(
                annotated, f"{vehicle_type.upper()} #{record_id} - APPROACHING",
                x1, y1 - 8, (255, 180, 0), scale=0.45
            )

    # Stage 2: YOLO26 is invoked only for vehicles that crossed the 70% line.
    # Its existing per-track cache/interval remains active after the crossing.
    special_started = time.perf_counter()
    with torch.inference_mode():
        specialised_regions = _detect_specialised_regions_for_vehicles(
            frame, analysis_candidates, plate_cache=plate_cache, frame_index=frame_index
        )
    special_ms = (time.perf_counter() - special_started) * 1000.0

    for confidence, vehicle_type, (x1, y1, x2, y2), track_id in candidates:
        record_id = int(track_id) if track_id is not None else int((x1 * 73856093 + y1 * 19349663 + x2 * 83492791 + y2 * 2654435761) % 1_000_000_000)
        # Specialised inference may have created this entry first.
        # setdefault does not populate missing keys in an existing dict.
        entry = plate_cache.setdefault(record_id, {})
        entry.setdefault("registration", "UNKNOWN")
        entry.setdefault("ocr_confidence", 0.0)
        entry.setdefault("attempts", 0)
        entry.setdefault("last_attempt", -10_000)
        entry.setdefault("bbox", None)
        entry.setdefault("stable", 0)
        entry.setdefault("last_seen", -10_000)
        entry.setdefault("ocr_status", "NOT_ATTEMPTED")
        entry.setdefault("plate_snippet", None)

        # Do not run cargo assessment, OCR, snapshot creation or persistence
        # before this vehicle reaches the 70% virtual detection line.
        if video and not entry.get("line_crossed", False):
            continue

        vehicle_box = (x1, y1, x2, y2)
        vehicle_crop = frame[y1:y2, x1:x2]
        # Separate visual cargo screening; vehicle YOLO and ByteTrack are unchanged.
        # Keep plate detections strictly inside the vehicle, but allow cargo
        # boxes immediately above/alongside the body so protruding loads are
        # not discarded before the geometry check.
        vehicle_special_regions = _regions_for_vehicle(
            specialised_regions, vehicle_box
        )
        cargo_regions = []
        for region in specialised_regions or []:
            name = str(region.get("class_name", "")).strip().lower()
            cargo_class = (
                name in {"cargo", "load", "luggage", "roof_load", "roof cargo"}
                or any(token in name for token in (
                    "cargo_load", "loaded_goods", "stacked_load",
                    "tyre_load", "tire_load", "roof_load",
                ))
            )
            if not cargo_class:
                continue
            rx1, ry1, rx2, ry2 = region.get("box", (0, 0, 0, 0))
            if float(region.get("confidence", 0.0)) < 0.35:
                continue

            pad_x = int((x2 - x1) * 0.25)
            pad_y = int((y2 - y1) * 0.50)
            # Cargo can extend above the roof or beyond the sides. Associate
            # it with this vehicle only when it is horizontally near the body
            # and not separated from it by a large vertical gap.
            horizontal_overlap = max(
                0, min(x2, rx2) - max(x1, rx1)
            )
            horizontal_near = (
                rx2 >= x1 - pad_x and rx1 <= x2 + pad_x
            )
            vertical_near = (
                ry2 >= y1 - pad_y and ry1 <= y2 + int((y2 - y1) * 0.20)
            )
            if horizontal_near and vertical_near and (
                horizontal_overlap > 0 or rx1 < x1 or rx2 > x2
            ):
                cargo_regions.append(region)

        cargo_assessment = assess_cargo_risk(
            vehicle_crop, vehicle_type, cargo_regions, vehicle_box=vehicle_box
        )
        if cargo_regions:
            logger.info(
                "[CARGO] tracker_id=%s candidates=%s vehicle_box=%s cargo_boxes=%s",
                record_id,
                len(cargo_regions),
                vehicle_box,
                [region.get("box") for region in cargo_regions],
            )
        # Confirm cargo protrusion against the vehicle detector's box.
        # A cargo box merely located on/inside a car is not overload evidence.
        protrusion = []
        for region in cargo_regions:
            rx1, ry1, rx2, ry2 = region["box"]
            protrudes_top = ry1 < y1 - max(4, int((y2 - y1) * 0.03))
            protrudes_left = rx1 < x1 - max(4, int((x2 - x1) * 0.03))
            protrudes_right = rx2 > x2 + max(4, int((x2 - x1) * 0.03))
            if protrudes_top or protrudes_left or protrudes_right:
                protrusion.append(region)
        if protrusion:
            cargo_assessment.update(
                cargo_risk_score=70,
                cargo_risk_level="WARNING",
                cargo_model_status="CONFIRMED_CARGO_PROTRUSION",
                cargo_indicators=["CARGO_EXTENDS_BEYOND_VEHICLE_BOUNDARY"],
            )
        else:
            cargo_assessment.update(
                cargo_risk_score=0,
                cargo_risk_level="NO_VISUAL_INDICATOR",
                cargo_model_status=(
                    "CARGO_DETECTED_WITHIN_VEHICLE_BOUNDARY"
                    if cargo_regions else "NO_CARGO_DETECTED"
                ),
                cargo_indicators=[],
            )

        should_attempt_ocr = (
            entry.get("registration", "UNKNOWN") in {"UNKNOWN", ""}
            and entry["attempts"] < MAX_OCR_ATTEMPTS
            and frame_index - entry["last_attempt"] >= OCR_RETRY_INTERVAL
        )
        if should_attempt_ocr:
            padded_crop, offset_x, offset_y = _padded_vehicle_crop(frame, vehicle_box)

            # YOLO26 specialised plate detections are in full-frame coordinates.
            # Convert the best plate box into padded-crop coordinates for OCR.
            plate_regions = [
                region for region in vehicle_special_regions
                if any(
                    token in str(region.get("class_name", "")).lower()
                    for token in ("plate", "licence", "license")
                )
            ]
            supplied_plate_bbox = None
            supplied_plate_confidence = 0.0
            if plate_regions:
                best_plate_region = max(
                    plate_regions, key=lambda item: item["confidence"]
                )
                rx1, ry1, rx2, ry2 = best_plate_region["box"]
                crop_left = x1 - offset_x
                crop_top = y1 - offset_y
                supplied_plate_bbox = (
                    rx1 - crop_left, ry1 - crop_top,
                    rx2 - crop_left, ry2 - crop_top,
                )
                supplied_plate_confidence = float(
                    best_plate_region.get("confidence", 0.0)
                )
                logger.info(
                    "[ANPR] YOLO26 plate candidate box=%s detector_confidence=%.3f",
                    supplied_plate_bbox,
                    supplied_plate_confidence,
                )

                # Save the localized detector crop independently of OCR.
                # This keeps the plate image visible even when text is UNKNOWN.
                if not entry.get("plate_snippet"):
                    entry["plate_snippet"] = _save_plate_candidate_crop(
                        frame, best_plate_region["box"], record_id
                    )

            registration, ocr_conf, plate_bbox, ocr_status, plate_snippet = _read_plate(
                padded_crop,
                plate_bbox=supplied_plate_bbox,
                plate_detection_confidence=supplied_plate_confidence,
                tracker_id=record_id,
            )
            logger.info(
                "[ANPR] primary OCR outcome registration=%s ocr_confidence=%.2f status=%s",
                registration, ocr_conf, ocr_status,
            )
            # Prefer the OCR service's refined crop; otherwise retain the
            # YOLO26 plate candidate crop saved above.
            if plate_snippet:
                entry["plate_snippet"] = plate_snippet
            plate_snippet = plate_snippet or entry.get("plate_snippet")
            bbox_offset_x, bbox_offset_y = offset_x, offset_y

            # The padded crop gives the plate detector context around the car.
            # If it fails, retry on the tighter vehicle crop; the bbox returned
            # by this second call is already relative to vehicle_crop.
            if registration == "UNKNOWN" and vehicle_crop.size:
                retry_registration, retry_conf, retry_bbox, retry_status, retry_snippet = _read_plate(
                    vehicle_crop, tracker_id=record_id
                )
                if retry_registration != "UNKNOWN":
                    registration, ocr_conf, plate_bbox, ocr_status = (
                        retry_registration, retry_conf, retry_bbox, retry_status
                    )
                    plate_snippet = retry_snippet or plate_snippet
                    if plate_snippet:
                        entry["plate_snippet"] = plate_snippet
                    bbox_offset_x, bbox_offset_y = 0, 0
                elif retry_conf > ocr_conf:
                    ocr_conf, plate_bbox, ocr_status = retry_conf, retry_bbox, retry_status
                    if retry_snippet:
                        plate_snippet = retry_snippet
                        entry["plate_snippet"] = retry_snippet
                    # If the best diagnostic bbox came from the tight crop,
                    # it must not be translated by the padded-crop offset.
                    bbox_offset_x, bbox_offset_y = 0, 0

            entry["attempts"] += 1
            entry["last_attempt"] = frame_index
            entry["ocr_status"] = ocr_status
            if plate_snippet:
                entry["plate_snippet"] = plate_snippet

            # Convert plate coordinates to the vehicle_crop coordinate system,
            # which is also the coordinate system used for the saved snapshot.
            if plate_bbox is not None:
                bx1, by1, bx2, by2 = [int(value) for value in plate_bbox]
                plate_bbox = (
                    bx1 - bbox_offset_x, by1 - bbox_offset_y,
                    bx2 - bbox_offset_x, by2 - bbox_offset_y,
                )

            if registration != "UNKNOWN":
                entry.update(
                    registration=registration,
                    ocr_confidence=ocr_conf,
                    bbox=plate_bbox,
                    plate_snippet=plate_snippet or entry.get("plate_snippet"),
                )
                logger.info(
                    "[ANPR] ACCEPTED tracker_id=%s registration=%s "
                    "ocr_confidence=%.2f%%",
                    record_id, registration, ocr_conf,
                )

        problem = _visibility_reason(vehicle_box, polygon, width, height)
        if confidence < MIN_CONFIDENCE:
            problem = problem or "LOW DETECTION CONFIDENCE"

        if not video:
            eligible = problem is None
        else:
            if entry["last_seen"] != frame_index - 1:
                entry["stable"] = 0
                entry["warning_stable"] = 0

            entry["last_seen"] = frame_index
            entry["stable"] = entry["stable"] + 1 if problem is None else 0
            eligible = problem is None and entry["stable"] >= STABLE_FRAMES

        # Confirmed unsafe cargo must not be discarded just because the
        # vehicle touches the frame edge or crosses the normal capture zone.
        # Keep a short stability requirement for live/video to avoid saving
        # one-frame false positives.
        current_classification = classify_overload(
            vehicle_type, x2 - x1, y2 - y1, cargo_assessment
        )
        if current_classification == "WARNING" and confidence >= MIN_CONFIDENCE:
            entry["warning_stable"] = entry.get("warning_stable", 0) + 1
        else:
            entry["warning_stable"] = 0

        warning_save_eligible = (
            current_classification == "WARNING"
            and confidence >= MIN_CONFIDENCE
            and (
                not video
                or entry.get("warning_stable", 0) >= STABLE_FRAMES
            )
        )
        save_eligible = eligible or warning_save_eligible

        if (
            problem is not None
            and warning_save_eligible
            and not eligible
        ):
            logger.warning(
                "[SAVE] Saving confirmed overload despite visibility gate: "
                "tracker_id=%s reason=%s",
                record_id, problem,
            )

        if save_eligible and record_id not in saved_tracks:
            snapshot_name = f"{uuid.uuid4().hex}.jpg"

            # One saved object = the vehicle together with its associated
            # detected cargo. The crop is the union of the vehicle box and
            # cargo boxes; plates and cargo are not saved as separate records.
            combined_x1, combined_y1 = x1, y1
            combined_x2, combined_y2 = x2, y2
            for cargo_region in cargo_regions:
                cx1, cy1, cx2, cy2 = map(int, cargo_region["box"])
                combined_x1 = min(combined_x1, cx1)
                combined_y1 = min(combined_y1, cy1)
                combined_x2 = max(combined_x2, cx2)
                combined_y2 = max(combined_y2, cy2)

            combined_x1 = max(0, min(combined_x1, width - 1))
            combined_y1 = max(0, min(combined_y1, height - 1))
            combined_x2 = max(combined_x1 + 1, min(combined_x2, width))
            combined_y2 = max(combined_y1 + 1, min(combined_y2, height))
            snapshot = frame[
                combined_y1:combined_y2, combined_x1:combined_x2
            ].copy()

            # Plate coordinates are stored relative to the original vehicle
            # crop. Translate them into the combined snapshot coordinate space.
            plate_bbox = entry.get("bbox")
            if plate_bbox is not None:
                try:
                    px1, py1, px2, py2 = [int(value) for value in plate_bbox]
                    px1 += x1 - combined_x1
                    px2 += x1 - combined_x1
                    py1 += y1 - combined_y1
                    py2 += y1 - combined_y1
                    if (
                        0 <= px1 < px2 <= snapshot.shape[1]
                        and 0 <= py1 < py2 <= snapshot.shape[0]
                    ):
                        cv2.rectangle(
                            snapshot, (px1, py1), (px2, py2), (0, 255, 255), 2
                        )
                except (ValueError, TypeError):
                    logger.debug("Could not draw plate box", exc_info=True)

            snapshot_url = None
            if cv2.imwrite(
                str(SNAPSHOT_DIR / snapshot_name), snapshot,
                [cv2.IMWRITE_JPEG_QUALITY, 85],
            ):
                snapshot_url = f"/uploads/snapshots/{snapshot_name}"

            saved_tracks.add(record_id)
            record = {
                "tracker_id": record_id,
                "vehicle_type": vehicle_type,
                "registration_number": entry["registration"],
                "plate_confidence": round(entry["ocr_confidence"], 2),
                "confidence": round(confidence * 100, 2),
                "overload_status": classify_overload(
                    vehicle_type, x2 - x1, y2 - y1, cargo_assessment
                ),
                "severity_score": float(cargo_assessment["cargo_risk_score"]),
                "cargo_risk_score": cargo_assessment["cargo_risk_score"],
                "cargo_risk_level": cargo_assessment["cargo_risk_level"],
                "cargo_indicators": cargo_assessment["cargo_indicators"],
                "cargo_assessment_method": cargo_assessment["cargo_assessment_method"],
                "specialised_detections": [
                    {
                        "class_name": region["class_name"],
                        "confidence": round(region["confidence"] * 100, 2),
                        "bbox": list(region["box"]),
                    }
                    for region in vehicle_special_regions
                ],
                "snapshot": snapshot_url,
                "plate_snippet": entry.get("plate_snippet"),
                "location": location,
            }
            detections.append(record)
            entry["saved_record"] = record
            entry["saved_snapshot"] = snapshot_url
            if records_by_id is not None:
                records_by_id[record_id] = record

            # Persist the first eligible observation. UNKNOWN is allowed so
            # a vehicle is not lost while ANPR is still retrying.
            try:
                save_or_update_detection(
                    tracker_id=int(record_id),
                    vehicle_type=vehicle_type.title(),
                    registration_number=entry["registration"],
                    confidence=round(confidence * 100, 2),
                    overload_status=record["overload_status"],
                    severity_score=int(record["severity_score"]),
                    location=location,
                    snapshot=snapshot_url,
                )
                _csv_upsert_vehicle(
                    tracker_id=int(record_id),
                    vehicle_type=vehicle_type.title(),
                    registration_number=entry["registration"],
                    confidence=round(confidence * 100, 2),
                    overload_status=record["overload_status"],
                    severity_score=int(record["severity_score"]),
                    location=location,
                    snapshot=snapshot_url,
                    plate_snippet=entry.get("plate_snippet"),
                )
            except Exception:
                logger.exception(
                    "[DATABASE] Could not save vehicle tracker_id=%s",
                    record_id,
                )

            stats["vehicles"] += 1
            if record["overload_status"] == "WARNING":
                stats["warnings"] += 1

        # A track may have been saved before the specialised model recognised
        # its cargo. Never leave that first NORMAL row unchanged: promote it
        # as soon as a later frame confirms visible cargo protrusion.
        existing_record = entry.get("saved_record")
        current_status = classify_overload(
            vehicle_type, x2 - x1, y2 - y1, cargo_assessment
        )
        if (
            record_id in saved_tracks
            and existing_record is not None
            and current_status == "WARNING"
            and existing_record.get("overload_status") != "WARNING"
        ):
            # Rebuild the snapshot from the union of vehicle and cargo boxes.
            snap_x1, snap_y1, snap_x2, snap_y2 = x1, y1, x2, y2
            for cargo_region in cargo_regions:
                cx1, cy1, cx2, cy2 = map(int, cargo_region["box"])
                snap_x1 = min(snap_x1, cx1)
                snap_y1 = min(snap_y1, cy1)
                snap_x2 = max(snap_x2, cx2)
                snap_y2 = max(snap_y2, cy2)

            snap_x1 = max(0, min(snap_x1, width - 1))
            snap_y1 = max(0, min(snap_y1, height - 1))
            snap_x2 = max(snap_x1 + 1, min(snap_x2, width))
            snap_y2 = max(snap_y1 + 1, min(snap_y2, height))
            combined_snapshot = frame[snap_y1:snap_y2, snap_x1:snap_x2].copy()

            new_snapshot_url = existing_record.get("snapshot")
            promoted_snapshot_name = f"{uuid.uuid4().hex}.jpg"
            if combined_snapshot.size and cv2.imwrite(
                str(SNAPSHOT_DIR / promoted_snapshot_name),
                combined_snapshot,
                [cv2.IMWRITE_JPEG_QUALITY, 88],
            ):
                new_snapshot_url = f"/uploads/snapshots/{promoted_snapshot_name}"

            existing_record.update({
                "overload_status": "WARNING",
                "severity_score": max(
                    float(existing_record.get("severity_score") or 0),
                    float(cargo_assessment.get("cargo_risk_score") or 70),
                    70.0,
                ),
                "cargo_risk_score": max(
                    int(existing_record.get("cargo_risk_score") or 0),
                    int(cargo_assessment.get("cargo_risk_score") or 70),
                    70,
                ),
                "cargo_risk_level": "WARNING",
                "cargo_indicators": cargo_assessment.get(
                    "cargo_indicators",
                    ["CARGO_EXTENDS_BEYOND_VEHICLE_BOUNDARY"],
                ),
                "snapshot": new_snapshot_url,
            })
            entry["saved_snapshot"] = new_snapshot_url

            try:
                save_or_update_detection(
                    tracker_id=int(record_id),
                    vehicle_type=vehicle_type.title(),
                    registration_number=entry.get("registration", "UNKNOWN"),
                    confidence=round(confidence * 100, 2),
                    overload_status="WARNING",
                    severity_score=int(existing_record["severity_score"]),
                    location=location,
                    snapshot=new_snapshot_url,
                )
                _csv_upsert_vehicle(
                    tracker_id=int(record_id),
                    vehicle_type=vehicle_type.title(),
                    registration_number=entry.get("registration", "UNKNOWN"),
                    confidence=round(confidence * 100, 2),
                    overload_status="WARNING",
                    severity_score=int(existing_record["severity_score"]),
                    location=location,
                    snapshot=new_snapshot_url,
                )
                logger.warning(
                    "[CARGO] PROMOTED tracker_id=%s to OVERLOAD; snapshot=%s",
                    record_id, new_snapshot_url,
                )
            except Exception:
                logger.exception(
                    "[CARGO] Could not persist overload promotion for tracker_id=%s",
                    record_id,
                )

        if records_by_id is not None and record_id in records_by_id:
            old_record = records_by_id[record_id]
            if entry["registration"] != "UNKNOWN":
                old_record["registration_number"] = entry["registration"]
                old_record["plate_confidence"] = round(entry["ocr_confidence"], 2)
                old_record["plate_snippet"] = entry.get("plate_snippet")

        # Link a localized plate crop to the already-saved vehicle row even
        # when OCR is not yet confident enough to confirm the registration.
        if entry.get("plate_snippet") and record_id in saved_tracks:
            try:
                _csv_update_plate_snippet(
                    tracker_id=int(record_id),
                    plate_snippet=entry["plate_snippet"],
                    location=location,
                )
                if records_by_id is not None and record_id in records_by_id:
                    records_by_id[record_id]["plate_snippet"] = entry["plate_snippet"]
            except Exception:
                logger.exception(
                    "[CSV] Could not link plate snippet for tracker_id=%s",
                    record_id,
                )

        # ANPR may succeed several frames after the vehicle was first saved.
        # Update the existing row for this tracker rather than inserting again.
        if entry["registration"] != "UNKNOWN" and record_id in saved_tracks:
            try:
                updated = update_detection_by_tracker_id(
                    tracker_id=int(record_id),
                    registration_number=entry["registration"],
                    location=location,
                )
                _csv_update_registration(
                    tracker_id=int(record_id),
                    registration_number=entry["registration"],
                    location=location,
                    plate_snippet=entry.get("plate_snippet"),
                )
                if updated is None:
                    logger.warning(
                        "[DATABASE] No recent row found to update for tracker_id=%s plate=%s",
                        record_id, entry["registration"],
                    )
            except Exception:
                logger.exception(
                    "[DATABASE] Could not update ANPR result for tracker_id=%s",
                    record_id,
                )

        saved_record = records_by_id.get(record_id, {}) if records_by_id is not None else {}
        current_status = saved_record.get(
            "overload_status",
            classify_overload(vehicle_type, x2 - x1, y2 - y1, cargo_assessment),
        )
        color = (0, 0, 255) if current_status == "WARNING" else ((0, 200, 0) if eligible else (0, 165, 255))

        # Same union geometry as the saved snapshot: truck + associated load.
        display_x1, display_y1 = x1, y1
        display_x2, display_y2 = x2, y2
        for cargo_region in cargo_regions:
            cx1, cy1, cx2, cy2 = map(int, cargo_region["box"])
            display_x1 = min(display_x1, cx1)
            display_y1 = min(display_y1, cy1)
            display_x2 = max(display_x2, cx2)
            display_y2 = max(display_y2, cy2)
        display_x1 = max(0, min(display_x1, width - 1))
        display_y1 = max(0, min(display_y1, height - 1))
        display_x2 = max(display_x1 + 1, min(display_x2, width))
        display_y2 = max(display_y1 + 1, min(display_y2, height))

        cv2.rectangle(
            annotated, (display_x1, display_y1),
            (display_x2, display_y2), color, 3
        )


    total_ms = (time.perf_counter() - frame_started) * 1000.0
    if video and frame_index > 0 and frame_index % PERFORMANCE_LOG_INTERVAL == 0:
        logger.info(
            "[PERFORMANCE] frame=%s vehicles=%s vehicle_model=%.1fms specialised=%.1fms total=%.1fms effective_fps=%.2f",
            frame_index, len(candidates), vehicle_ms, special_ms, total_ms,
            1000.0 / max(total_ms, 1.0),
        )

    stats["processing_ms"] = round(total_ms, 1)
    stats["processing_fps"] = round(1000.0 / max(total_ms, 1.0), 2)
    stats["vehicle_inference_ms"] = round(vehicle_ms, 1)
    stats["specialised_inference_ms"] = round(special_ms, 1)
    return annotated, detections, stats


def detect_objects(
    image_source,
    location="Polokwane Weighbridge",
    saved_tracks=None,
    plate_cache=None,
    frame_index=0,
):
    """Detect vehicles and number plates in an image or NumPy frame."""
    if isinstance(image_source, (str, Path)):
        frame = cv2.imread(str(image_source))
        if frame is None:
            raise ValueError(f"Cannot read image: {image_source}")
    else:
        if not isinstance(image_source, np.ndarray) or image_source.size == 0:
            raise ValueError("image_source must be a valid image path or NumPy frame")
        frame = image_source.copy()

    if saved_tracks is None:
        saved_tracks = set()
    if plate_cache is None:
        plate_cache = {}

    video = not isinstance(image_source, (str, Path)) and frame_index > 0

    annotated, vehicles, stats = _analyse_frame(
        frame,
        location,
        saved_tracks,
        plate_cache,
        frame_index,
        video=video,
    )

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"detected_{uuid.uuid4().hex}.jpg"
    output_path = IMAGE_DIR / filename

    if not cv2.imwrite(
        str(output_path),
        annotated,
        [cv2.IMWRITE_JPEG_QUALITY, 85],
    ):
        raise IOError(f"Could not save annotated image: {output_path}")

    return {
        "annotated_image": f"/uploads/images/{filename}",
        "vehicles": vehicles,
        "stats": stats,
    }


def detect_frame(
    frame,
    location="Polokwane Weighbridge",
    saved_tracks=None,
    plate_cache=None,
    frame_index=1,
    records_by_id=None,
):
    """Process one video frame and return its annotations and detections."""
    if not isinstance(frame, np.ndarray) or frame.size == 0:
        raise ValueError("frame must be a valid NumPy image")

    if saved_tracks is None:
        saved_tracks = set()
    if plate_cache is None:
        plate_cache = {}

    annotated, vehicles, _ = _analyse_frame(
        frame,
        location,
        saved_tracks,
        plate_cache,
        frame_index,
        video=True,
        records_by_id=records_by_id,
    )

    return annotated, vehicles


def process_image(
    image_path,
    location="Polokwane Weighbridge",
):
    """Process a single uploaded image."""
    return detect_objects(image_path, location)


def process_video(
    video_path,
    processed_video,
    location="Polokwane Weighbridge",
    progress_callback=None,
    frame_callback=None,
    cancel_event=None,
):
    """Process an uploaded video with OpenCV only (no HLS or FFmpeg CLI).

    Frames are read with VideoCapture, analysed by the existing YOLO pipeline,
    and written directly to one annotated MP4 with VideoWriter.
    """
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"Could not open uploaded video: {video_path}")

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

    if not np.isfinite(fps) or fps <= 0:
        fps = 25.0
    if width <= 0 or height <= 0:
        capture.release()
        raise ValueError("Uploaded video has invalid dimensions")

    processed_video = Path(processed_video)
    processed_video.parent.mkdir(parents=True, exist_ok=True)

    writer = cv2.VideoWriter(
        str(processed_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        writer.release()
        raise IOError(
            "OpenCV could not create the annotated MP4. Check that the output "
            "folder is writable and that this OpenCV build supports MP4 writing."
        )

    saved_tracks = set()
    plate_cache = {}
    records_by_id = {}
    vehicles = []
    frame_index = 0
    last_reported_progress = -1

    try:
        # Reset ByteTrack state so IDs from a previous upload are not reused.
        predictor = getattr(vehicle_model, "predictor", None)
        if predictor is not None:
            for tracker in getattr(predictor, "trackers", []):
                if hasattr(tracker, "reset"):
                    tracker.reset()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                break

            ok, frame = capture.read()
            if not ok:
                break

            if cancel_event is not None and cancel_event.is_set():
                break

            frame_index += 1
            annotated, new_vehicles = detect_frame(
                frame=frame,
                location=location,
                saved_tracks=saved_tracks,
                plate_cache=plate_cache,
                frame_index=frame_index,
                records_by_id=records_by_id,
            )

            if annotated.shape[:2] != (height, width):
                annotated = cv2.resize(
                    annotated, (width, height), interpolation=cv2.INTER_LINEAR
                )

            writer.write(annotated)
            vehicles.extend(new_vehicles)

            # Publish the newest annotated frame to the job's bounded live stream.
            # The callback must retain only the latest frame (not an unbounded queue).
            if frame_callback is not None:
                try:
                    frame_callback(annotated, frame_index, total_frames, fps)
                except Exception:
                    logger.exception("Could not publish annotated video frame %s", frame_index)

            if total_frames > 0:
                progress = min(99, int(frame_index * 100 / total_frames))
            else:
                # Some containers do not report frame count. Keep progress
                # indeterminate until EOF rather than failing the upload.
                progress = min(99, frame_index)

            if progress != last_reported_progress:
                last_reported_progress = progress
                if progress_callback:
                    progress_callback(progress, progress >= 25)

        if cancel_event is not None and cancel_event.is_set():
            try:
                processed_video.unlink(missing_ok=True)
            except Exception:
                logger.debug("Could not remove cancelled partial video: %s", processed_video)
            raise InterruptedError("Video source was cancelled")
        if frame_index == 0:
            raise ValueError("No readable frames were found in the uploaded video")

    finally:
        capture.release()
        writer.release()

    if not processed_video.exists() or processed_video.stat().st_size == 0:
        raise IOError("The annotated video could not be saved")

    if progress_callback:
        progress_callback(100, True)

    warning_count = sum(
        str(v.get("overload_status", "")).upper() in {"WARNING", "HIGH", "OVERLOADED", "CRITICAL"}
        for v in vehicles
    )
    review_count = sum(
        str(v.get("overload_status", "")).upper() in {"REVIEW_REQUIRED", "REVIEW", "INDETERMINATE", "UNKNOWN"}
        for v in vehicles
    )

    return {
        "processed_video": f"/uploads/videos/{processed_video.name}",
        "vehicles": vehicles,
        "stats": {
            "vehicles": len(vehicles),
            "warnings": review_count,
            "overloaded": warning_count,
            "frames_processed": frame_index,
            "fps": fps,
        },
    }

from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Event
from typing import Any
import shutil
from services.camera_service import generate_frames
from services.location_service import get_location_name

# ------------------------------------------------------------
# DIRECTORIES
# ------------------------------------------------------------

UPLOAD_DIR = BASE_DIR / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"
PLATE_DIR = UPLOAD_DIR / "plates"

for directory in (UPLOAD_DIR, IMAGE_DIR, VIDEO_DIR, SNAPSHOT_DIR, PLATE_DIR):
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

# A source switch cancels the active virtual-camera job. Events are checked
# between frames so the previous source stops promptly without building a queue.
JOB_CANCEL_EVENTS: dict[str, Event] = {}
JOB_FUTURES: dict[str, Any] = {}
active_video_lock = Lock()
ACTIVE_VIDEO_JOB_ID: str | None = None

# One JPEG frame per uploaded-video job. This is deliberately a latest-frame
# buffer, not a queue, so slow browsers never build up seconds of stale footage.
JOB_STREAMS: dict[str, dict[str, Any]] = {}
stream_lock = Lock()


def publish_video_frame(job_id: str, frame, frame_index: int, total_frames: int, fps: float) -> None:
    ok, encoded = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78]
    )
    if not ok:
        return

    with stream_lock:
        stream = JOB_STREAMS.setdefault(job_id, {"jpeg": None, "frame_index": 0})
        stream["jpeg"] = encoded.tobytes()
        stream["frame_index"] = frame_index

    progress = min(99, int(frame_index * 100 / total_frames)) if total_frames > 0 else 0
    update_job(
        job_id,
        progress=progress,
        preview_ready=True,
        frames_processed=frame_index,
        source_fps=fps,
    )


def job_video_mjpeg(job_id: str):
    """Yield only the newest annotated frame for a video-as-camera stream."""
    last_frame_index = -1
    while True:
        with stream_lock:
            stream = JOB_STREAMS.get(job_id)
            frame_index = stream.get("frame_index", -1) if stream else -1
            jpeg = stream.get("jpeg") if stream else None

        if jpeg is not None and frame_index != last_frame_index:
            last_frame_index = frame_index
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n"
                + jpeg
                + b"\r\n"
            )

        with jobs_lock:
            job = JOBS.get(job_id, {})
            status = job.get("status")

        # Deliver the final frame above, then close the stream. The frontend
        # switches to the completed annotated MP4 for replay/seeking.
        if status in {"completed", "failed", "cancelled"} and (jpeg is None or frame_index == last_frame_index):
            break

        time.sleep(0.03)

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
    with jobs_lock:
        if JOBS.get(job_id, {}).get("status") == "cancelled":
            return
    update_job(job_id, status="processing")

    try:
        result = process_image(str(image_path), location)
        with jobs_lock:
            if JOBS.get(job_id, {}).get("status") == "cancelled":
                return

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


def cancel_video_job(job_id: str, reason: str = "Video source changed") -> None:
    """Stop a video source, cancel queued work and discard its latest frame."""
    event = JOB_CANCEL_EVENTS.get(job_id)
    if event is not None:
        event.set()

    future = JOB_FUTURES.get(job_id)
    if future is not None:
        future.cancel()  # succeeds immediately if the task has not started

    update_job(job_id, status="cancelled", cancel_reason=reason)
    with stream_lock:
        JOB_STREAMS.pop(job_id, None)


def run_video_job(
    job_id: str,
    input_path: Path,
    processed_path: Path,
    location: str,
    cancel_event: Event,
) -> None:
    if cancel_event.is_set():
        update_job(job_id, status="cancelled")
        return
    update_job(job_id, status="processing")

    try:
        def progress_callback(progress, preview_ready):
            update_job(
                job_id,
                progress=int(progress),
                preview_ready=bool(preview_ready),
            )

        def frame_callback(frame, frame_index, total_frames, fps):
            publish_video_frame(job_id, frame, frame_index, total_frames, fps)

        result = process_video(
            str(input_path),
            str(processed_path),
            location,
            progress_callback=progress_callback,
            frame_callback=frame_callback,
            cancel_event=cancel_event,
        )

        if cancel_event.is_set():
            update_job(job_id, status="cancelled", progress=0, preview_ready=False)
            return

        update_job(
            job_id,
            status="completed",
            progress=100,
            preview_ready=True,
            result={
                "processed_video": result.get("processed_video"),
                "vehicles": result.get("vehicles", []),
                "stats": result.get("stats", {}),
            },
        )
    except InterruptedError:
        update_job(job_id, status="cancelled", progress=0, preview_ready=False)
    except Exception as exc:
        if cancel_event.is_set():
            update_job(job_id, status="cancelled", progress=0, preview_ready=False)
        else:
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

    JOB_FUTURES[job_id] = inference_executor.submit(
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
    global ACTIVE_VIDEO_JOB_ID
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".mp4", ".mov", ".avi", ".mkv"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format.",
        )

    # A newly selected video replaces the previous video source immediately.
    with active_video_lock:
        previous_job_id = ACTIVE_VIDEO_JOB_ID
        ACTIVE_VIDEO_JOB_ID = None
    if previous_job_id:
        cancel_video_job(previous_job_id, "Replaced by a newer video source")

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
    cancel_event = Event()
    JOB_CANCEL_EVENTS[job_id] = cancel_event
    preview_url = f"/uploads/videos/{filename}"

    create_job(
        job_id,
        {
            "status": "queued",
            "type": "video",
            "preview_video": preview_url,
            "stream_url": f"/jobs/{job_id}/stream",
            "progress": 0,
            "preview_ready": False,
            "result": None,
            "error": None,
        },
    )

    with stream_lock:
        JOB_STREAMS[job_id] = {"jpeg": None, "frame_index": 0}

    with active_video_lock:
        ACTIVE_VIDEO_JOB_ID = job_id
    future = inference_executor.submit(
        run_video_job,
        job_id,
        input_path,
        processed_path,
        location,
        cancel_event,
    )
    JOB_FUTURES[job_id] = future

    return {
        "job_id": job_id,
        "status": "queued",
        "preview_video": preview_url,
        "stream_url": f"/jobs/{job_id}/stream",
        "message": "Video connected as a virtual camera. Live detection is starting.",
    }


# ------------------------------------------------------------
# CANCEL / REPLACE CURRENT PROCESSING SOURCE
# ------------------------------------------------------------

@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with jobs_lock:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Processing job not found.")
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return {"job_id": job_id, "status": job.get("status")}

    cancel_video_job(job_id, "Cancelled because the camera source changed")
    return {"job_id": job_id, "status": "cancelled"}


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
# UPLOADED VIDEO AS A VIRTUAL CAMERA STREAM
# ------------------------------------------------------------

@app.get("/jobs/{job_id}/stream")
def uploaded_video_stream(job_id: str):
    with jobs_lock:
        if job_id not in JOBS:
            raise HTTPException(status_code=404, detail="Video job not found.")
        if JOBS[job_id].get("type") != "video":
            raise HTTPException(status_code=400, detail="This job is not a video source.")

    return StreamingResponse(
        job_video_mjpeg(job_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------
# LIVE PHYSICAL CAMERA STREAM
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