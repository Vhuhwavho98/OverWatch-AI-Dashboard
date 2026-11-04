
from __future__ import annotations
import logging
import uuid
from pathlib import Path
import cv2
import numpy as np
import torch
from ultralytics import YOLO
import re
from services.ocr_service import detect_plate
from database.repository import save_or_update_detection, update_detection_by_tracker_id

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "models" / "yolo11n.pt"
UPLOAD_DIR = BASE_DIR / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"

for directory in (IMAGE_DIR, VIDEO_DIR, SNAPSHOT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

if not MODEL_PATH.is_file():
    raise FileNotFoundError(f"YOLO model missing: {MODEL_PATH}")

model = YOLO(str(MODEL_PATH))
DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"

VEHICLE_CLASS_IDS = [1, 2, 3, 5, 7]

VEHICLE_CLASSES = {
    "bicycle",
    "car",
    "motorcycle",
    "bus",
    "truck",
}

# Conservative speed/accuracy defaults; adjust only after testing.
YOLO_IMAGE_SIZE = 640
YOLO_CONFIDENCE = 0.40
YOLO_IOU = 0.50
MAX_DETECTIONS = 30

DETECTION_POLYGON = np.array(
    [[20, 90], [1260, 90], [1235, 700], [40, 700]], dtype=np.int32
)
REFERENCE_WIDTH, REFERENCE_HEIGHT = 1280, 720
FRAME_MARGIN_FRACTION = 0.025
STABLE_FRAMES = 4
MIN_CONFIDENCE = 0.40
MAX_OCR_ATTEMPTS = 8
OCR_RETRY_INTERVAL = 12


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


def assess_cargo_risk(vehicle_crop, vehicle_type="car"):
    """Conservative OpenCV cargo screening; never estimates actual weight."""
    result = {
        "cargo_risk_score": 0,
        "cargo_risk_level": "NO_VISUAL_INDICATOR",
        "cargo_indicators": [],
        "cargo_assessment_method": "opencv_heuristic_v1",
    }
    if (vehicle_crop is None or not isinstance(vehicle_crop, np.ndarray)
            or vehicle_crop.size == 0 or vehicle_crop.ndim < 2):
        result.update(cargo_risk_level="NOT_ASSESSED",
                      cargo_indicators=["INVALID_VEHICLE_CROP"])
        return result

    h, w = vehicle_crop.shape[:2]
    if h < 50 or w < 50:
        result.update(cargo_risk_level="NOT_ASSESSED",
                      cargo_indicators=["VEHICLE_CROP_TOO_SMALL"])
        return result

    upper = vehicle_crop[:max(1, int(h * 0.42)), :]
    gray = cv2.cvtColor(upper, cv2.COLOR_BGR2GRAY) if upper.ndim == 3 else upper
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 55, 145)
    edge_density = float(np.count_nonzero(edges)) / float(edges.size)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    upper_area = max(1, upper.shape[0] * upper.shape[1])
    significant = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < upper_area * 0.008:
            continue
        x, y, cw, ch = cv2.boundingRect(contour)
        if ch >= max(8, int(upper.shape[0] * 0.12)):
            significant.append((area, x, y, cw, ch))

    score, indicators = 0, []
    # Heuristic indicators only; thresholds require dataset validation.
    if edge_density >= 0.105:
        score += 25
        indicators.append("HIGH_VISUAL_COMPLEXITY_IN_UPPER_REGION")
    if len(significant) >= 3:
        score += 25
        indicators.append("MULTIPLE_LARGE_STRUCTURES_IN_UPPER_REGION")
    if significant and max(item[4] for item in significant) >= upper.shape[0] * 0.45:
        score += 20
        indicators.append("VERTICALLY_EXTENDED_UPPER_STRUCTURE")

    score = min(100, score)
    result.update(
        cargo_risk_score=score,
        cargo_risk_level="REVIEW_REQUIRED" if score >= 25 else "NO_VISUAL_INDICATOR",
        cargo_indicators=indicators,
        cargo_edge_density=round(edge_density, 4),
        cargo_candidate_regions=len(significant),
    )
    return result


def classify_overload(vehicle_type, width, height, cargo_assessment=None):
    """Map visual screening to status; NORMAL is not proof of legal loading."""
    if not cargo_assessment or cargo_assessment.get("cargo_risk_level") == "NOT_ASSESSED":
        return "REVIEW_REQUIRED"
    if cargo_assessment.get("cargo_risk_level") == "REVIEW_REQUIRED":
        return "WARNING"
    return "NORMAL"

def _read_plate(crop):
    if crop is None or not isinstance(crop, np.ndarray) or crop.size == 0:
        return "UNKNOWN", 0.0, None, "OCR_FAILED"

    try:
        result = detect_plate(crop, debug=True)
        if not isinstance(result, dict) or not result:
            return "UNKNOWN", 0.0, None, "NO_PLATE_BOX"

        raw_registration = str(result.get("registration") or "").strip().upper()
        registration = re.sub(r"[^A-Z0-9]", "", raw_registration)
        ocr_confidence = float(result.get("ocr_confidence") or 0.0)
        bbox = result.get("bbox")
        status = str(result.get("status") or "OCR_FAILED").upper()

        letters = sum(ch.isalpha() for ch in registration)
        digits = sum(ch.isdigit() for ch in registration)
        valid_candidate = (
            5 <= len(registration) <= 10
            and letters >= 2
            and digits >= 2
            and registration not in {"OCRNOTREADABLE", "UNKNOWN"}
        )

        # Keep uncertain text out of the database as a confirmed plate, but log
        # the detector/OCR outcome so a failed read can be diagnosed.
        if not valid_candidate or status != "READ" or ocr_confidence < 20.0:
            logger.info(
                "[ANPR] plate not confirmed: raw=%r confidence=%.2f status=%s bbox=%s",
                raw_registration, ocr_confidence, status, bbox,
            )
            return "UNKNOWN", ocr_confidence, bbox, (
                "NO_PLATE_BOX" if status == "NO_PLATE_BOX" else "NEEDS_REVIEW"
            )

        logger.info(
            "[ANPR] plate read: %s confidence=%.2f bbox=%s",
            registration, ocr_confidence, bbox,
        )
        return registration, ocr_confidence, bbox, "READ"

    except Exception:
        logger.exception("Number plate OCR failed")
        return "UNKNOWN", 0.0, None, "OCR_EXCEPTION"


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return overlap / max(1, area_a + area_b - overlap)


def _collect_candidates(results, frame_w, frame_h):
    candidates = []

    for result in results:
        if result.boxes is None:
            continue

        for box in result.boxes:
            class_id = int(box.cls[0].item())
            vehicle_type = str(model.names[class_id]).lower()

            if vehicle_type not in VEHICLE_CLASSES:
                continue

            confidence = float(box.conf[0].item())
            x1, y1, x2, y2 = [
                int(value) for value in box.xyxy[0].tolist()
            ]

            x1 = max(0, min(x1, frame_w - 1))
            y1 = max(0, min(y1, frame_h - 1))
            x2 = max(0, min(x2, frame_w))
            y2 = max(0, min(y2, frame_h))

            if x2 - x1 < 35 or y2 - y1 < 35:
                continue

            track_id = (
                int(box.id.item())
                if box.id is not None
                else None
            )

            candidates.append(
                (confidence, vehicle_type, (x1, y1, x2, y2), track_id)
            )

    candidates.sort(key=lambda item: item[0], reverse=True)
    accepted = []

    for candidate in candidates:
        confidence, vehicle_type, box, track_id = candidate
        x1, y1, x2, y2 = box
        area = max(1, (x2 - x1) * (y2 - y1))

        duplicate = False

        for previous in accepted:
            _, previous_type, previous_box, _ = previous

            if vehicle_type != previous_type:
                continue

            px1, py1, px2, py2 = previous_box

            ix1 = max(x1, px1)
            iy1 = max(y1, py1)
            ix2 = min(x2, px2)
            iy2 = min(y2, py2)

            intersection = (
                max(0, ix2 - ix1) * max(0, iy2 - iy1)
            )

            previous_area = max(
                1, (px2 - px1) * (py2 - py1)
            )

            union = area + previous_area - intersection
            iou = intersection / max(1, union)

            # Detect a small box largely contained within a
            # larger box of the same class.
            containment = intersection / min(area, previous_area)

            if iou > 0.60 or containment > 0.90:
                duplicate = True
                break

        if not duplicate:
            accepted.append(candidate)

    return accepted


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

    # Do not pass the deprecated Ultralytics `half` argument. Ultralytics
    # chooses a supported inference precision automatically for this install.
    with torch.inference_mode():
        if video:
            results = model.track(
                frame, persist=True, tracker="bytetrack.yaml",
                imgsz=YOLO_IMAGE_SIZE, conf=YOLO_CONFIDENCE, iou=YOLO_IOU,
                classes=VEHICLE_CLASS_IDS, max_det=MAX_DETECTIONS,
                device=DEVICE, verbose=False,
            )
        else:
            results = model.predict(
                frame, imgsz=YOLO_IMAGE_SIZE, conf=YOLO_CONFIDENCE, iou=YOLO_IOU,
                classes=VEHICLE_CLASS_IDS, max_det=MAX_DETECTIONS,
                device=DEVICE, verbose=False,
            )

    detections = []
    stats = {"vehicles": 0, "warnings": 0, "overloaded": 0}
    candidates = _collect_candidates(results, width, height)

    for confidence, vehicle_type, (x1, y1, x2, y2), track_id in candidates:
        record_id = int(track_id) if track_id is not None else int((x1 * 73856093 + y1 * 19349663 + x2 * 83492791 + y2 * 2654435761) % 1_000_000_000)
        entry = plate_cache.setdefault(
            record_id,
            {
                "registration": "UNKNOWN",
                "ocr_confidence": 0.0,
                "attempts": 0,
                "last_attempt": -10_000,
                "bbox": None,
                "stable": 0,
                "last_seen": -10_000,
                "ocr_status": "NOT_ATTEMPTED",
            },
        )

        vehicle_box = (x1, y1, x2, y2)
        vehicle_crop = frame[y1:y2, x1:x2]
        # Separate visual cargo screening; vehicle YOLO and ByteTrack are unchanged.
        cargo_assessment = assess_cargo_risk(vehicle_crop, vehicle_type)

        should_attempt_ocr = (
            entry["registration"] in {"UNKNOWN", ""}
            and entry["attempts"] < MAX_OCR_ATTEMPTS
            and frame_index - entry["last_attempt"] >= OCR_RETRY_INTERVAL
        )
        if should_attempt_ocr:
            padded_crop, offset_x, offset_y = _padded_vehicle_crop(frame, vehicle_box)
            registration, ocr_conf, plate_bbox, ocr_status = _read_plate(padded_crop)
            bbox_offset_x, bbox_offset_y = offset_x, offset_y

            # The padded crop gives the plate detector context around the car.
            # If it fails, retry on the tighter vehicle crop; the bbox returned
            # by this second call is already relative to vehicle_crop.
            if registration == "UNKNOWN" and vehicle_crop.size:
                retry_registration, retry_conf, retry_bbox, retry_status = _read_plate(vehicle_crop)
                if retry_registration != "UNKNOWN":
                    registration, ocr_conf, plate_bbox, ocr_status = (
                        retry_registration, retry_conf, retry_bbox, retry_status
                    )
                    bbox_offset_x, bbox_offset_y = 0, 0
                elif retry_conf > ocr_conf:
                    ocr_conf, plate_bbox, ocr_status = retry_conf, retry_bbox, retry_status
                    # If the best diagnostic bbox came from the tight crop,
                    # it must not be translated by the padded-crop offset.
                    bbox_offset_x, bbox_offset_y = 0, 0

            entry["attempts"] += 1
            entry["last_attempt"] = frame_index
            entry["ocr_status"] = ocr_status

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
                )

        problem = _visibility_reason(vehicle_box, polygon, width, height)
        if confidence < MIN_CONFIDENCE:
            problem = problem or "LOW DETECTION CONFIDENCE"

        if not video:
            eligible = problem is None
        else:
            if entry["last_seen"] != frame_index - 1:
                entry["stable"] = 0
            entry["last_seen"] = frame_index
            entry["stable"] = entry["stable"] + 1 if problem is None else 0
            eligible = problem is None and entry["stable"] >= STABLE_FRAMES
            if problem is None and not eligible:
                problem = "WAITING FOR COMPLETE VEHICLE"

        if eligible and record_id not in saved_tracks:
            snapshot_name = f"{uuid.uuid4().hex}.jpg"
            snapshot = vehicle_crop.copy()
            plate_bbox = entry.get("bbox")
            if plate_bbox is not None:
                try:
                    px1, py1, px2, py2 = [int(value) for value in plate_bbox]
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
                "snapshot": snapshot_url,
                "location": location,
            }
            detections.append(record)
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
            except Exception:
                logger.exception(
                    "[DATABASE] Could not save vehicle tracker_id=%s",
                    record_id,
                )

            stats["vehicles"] += 1
            if record["overload_status"] == "WARNING":
                stats["warnings"] += 1

        if records_by_id is not None and record_id in records_by_id:
            old_record = records_by_id[record_id]
            if entry["registration"] != "UNKNOWN":
                old_record["registration_number"] = entry["registration"]
                old_record["plate_confidence"] = round(entry["ocr_confidence"], 2)

        # ANPR may succeed several frames after the vehicle was first saved.
        # Update the existing row for this tracker rather than inserting again.
        if entry["registration"] != "UNKNOWN" and record_id in saved_tracks:
            try:
                updated = update_detection_by_tracker_id(
                    tracker_id=int(record_id),
                    registration_number=entry["registration"],
                    location=location,
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

        color = (0, 200, 0) if eligible else (0, 165, 255)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
        _label(annotated, f"{vehicle_type.upper()} | ID:{record_id}", x1, y1 - 8, color)
        cargo_label = (
            f"CARGO REVIEW: {cargo_assessment['cargo_risk_score']}/100"
            if cargo_assessment["cargo_risk_level"] == "REVIEW_REQUIRED"
            else "NO CARGO INDICATOR"
        )
        cargo_color = (
            (0, 165, 255)
            if cargo_assessment["cargo_risk_level"] == "REVIEW_REQUIRED"
            else (180, 180, 180)
        )
        _label(annotated, cargo_label, x1, min(height - 44, y2 + 44), cargo_color, 0.42)
        status_text = (
            records_by_id[record_id].get("overload_status", "NORMAL")
            if records_by_id is not None and record_id in records_by_id
            else "NORMAL"
        )
        _label(
            annotated,
            status_text if eligible else (problem or "CHECKING"),
            x1, min(height - 24, y2 + 20), color,
        )
        if entry["registration"] not in {"UNKNOWN", ""}:
            _label(
                annotated,
                f"PLATE: {entry['registration']} ({entry['ocr_confidence']:.0f}%)",
                x1, min(height - 4, y2 + 40), (255, 255, 255),
            )
        elif entry["ocr_status"] not in {"NOT_ATTEMPTED", "NO_PLATE_BOX"}:
            _label(
                annotated, f"PLATE: {entry['ocr_status']}",
                x1, min(height - 4, y2 + 40), (255, 255, 255),
            )

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
):
    """Process a video, run vehicle/plate detection, and save the output."""
    capture = cv2.VideoCapture(str(video_path))

    if not capture.isOpened():
        capture.release()
        raise ValueError(f"Could not open video: {video_path}")

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))

    if not np.isfinite(fps) or fps <= 0:
        fps = 25.0

    if width <= 0 or height <= 0:
        capture.release()
        raise ValueError("Video has invalid dimensions")

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
            f"Could not create processed video: {processed_video}"
        )

    saved_tracks = set()
    plate_cache = {}
    records_by_id = {}
    vehicles = []

    try:
        # Reset any existing YOLO tracking state before processing.
        predictor = getattr(model, "predictor", None)

        if predictor is not None:
            for tracker in getattr(predictor, "trackers", []):
                if hasattr(tracker, "reset"):
                    tracker.reset()

        frame_index = 0

        while True:
            ok, frame = capture.read()

            if not ok:
                break

            frame_index += 1

            # FIX: pass frame as the first argument.
            annotated, new_vehicles = detect_frame(
                frame=frame,
                location=location,
                saved_tracks=saved_tracks,
                plate_cache=plate_cache,
                frame_index=frame_index,
                records_by_id=records_by_id,
            )

            # VideoWriter requires frames of the configured dimensions.
            if annotated.shape[:2] != (height, width):
                annotated = cv2.resize(
                    annotated,
                    (width, height),
                    interpolation=cv2.INTER_LINEAR,
                )

            writer.write(annotated)
            vehicles.extend(new_vehicles)

    finally:
        capture.release()
        writer.release()

    return {
        "processed_video": f"/uploads/videos/{processed_video.name}",
        "vehicles": vehicles,
        "stats": {
            "vehicles": len(vehicles),
            "warnings": sum(
                v.get("overload_status") == "WARNING"
                for v in vehicles
            ),
            "overloaded": sum(
                v.get("overload_status") == "OVERLOADED"
                for v in vehicles
            ),
        },
    }
