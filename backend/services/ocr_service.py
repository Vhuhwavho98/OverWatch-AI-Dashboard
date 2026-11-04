from __future__ import annotations
import argparse
import logging
import re
import time
from pathlib import Path
from typing import Any

import cv2
import easyocr
import numpy as np
import torch
from ultralytics import YOLO

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"
LOCAL_MODEL_PATHS = (
    MODELS_DIR / "license_plate_detector.pt",
)
HF_REPO = "morsetechlab/yolov11-license-plate-detection"
HF_FILENAME = "license-plate-finetune-v1n.pt"

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
MIN_OCR_CONFIDENCE = 0.45
MIN_PLATE_WIDTH = 20
MIN_PLATE_HEIGHT = 7
MAX_CANDIDATES = 8

MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _load_plate_model():
    """Load the dedicated plate model; never load/replace the vehicle model."""
    for model_path in LOCAL_MODEL_PATHS:
        if model_path.is_file():
            try:
                model = YOLO(str(model_path))
                if DEVICE.startswith("cuda"):
                    model.to(DEVICE)
                logger.info("ANPR plate detector loaded from %s", model_path)
                return model
            except Exception:
                logger.exception("Could not load ANPR model %s", model_path)

    try:
        from huggingface_hub import hf_hub_download

        model_path = hf_hub_download(
            repo_id=HF_REPO,
            filename=HF_FILENAME,
            repo_type="model",
        )
        model = YOLO(model_path)
        if DEVICE.startswith("cuda"):
            model.to(DEVICE)
        logger.info("ANPR plate detector loaded from Hugging Face: %s", model_path)
        return model
    except Exception:
        logger.exception("Could not load/download ANPR weights; OpenCV fallback will be used")
        return None


plate_model = _load_plate_model()
reader = easyocr.Reader(
    ["en"],
    gpu=torch.cuda.is_available(),
    detector=False,
    recognizer=True,
    verbose=False,
)


def _valid_image(image: Any) -> bool:
    return isinstance(image, np.ndarray) and image.size > 0 and image.ndim in (2, 3)


def _clamp_box(box, width: int, height: int):
    x1, y1, x2, y2 = [int(round(float(v))) for v in box]
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    bw, bh = x2 - x1, y2 - y1
    if bw < MIN_PLATE_WIDTH or bh < MIN_PLATE_HEIGHT:
        return None
    ratio = bw / max(1, bh)
    # Reject obvious non-plate regions (e.g. near-square false detections).
    if not 1.45 <= ratio <= 8.5:
        return None
    return x1, y1, x2, y2


def _yolo_boxes(image: np.ndarray, imgsz: int):
    if plate_model is None:
        return []
    try:
        with torch.inference_mode():
            result = plate_model.predict(
                source=image,
                imgsz=imgsz,
                conf=0.06,
                iou=0.45,
                max_det=MAX_CANDIDATES,
                device=DEVICE,
                verbose=False,
            )[0]
        if result.boxes is None:
            return []
        h, w = image.shape[:2]
        candidates = []
        for box in result.boxes:
            coords = _clamp_box(box.xyxy[0].tolist(), w, h)
            if coords is None:
                continue
            confidence = float(box.conf[0].item())
            candidates.append((coords, confidence, "YOLO", imgsz))
        return sorted(candidates, key=lambda item: item[1], reverse=True)
    except Exception:
        logger.exception("ANPR YOLO plate detection failed")
        return []


def _opencv_boxes(image: np.ndarray):
    """Fallback plate proposals based on rectangular edge/contour structure."""
    if not _valid_image(image):
        return []
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    blurred = cv2.bilateralFilter(gray, 7, 45, 45)
    edges = cv2.Canny(blurred, 40, 160)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    proposals = []
    image_area = max(1, w * h)
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        ratio = bw / max(1, bh)
        area_ratio = (bw * bh) / image_area
        if not 1.6 <= ratio <= 7.5 or not 0.0008 <= area_ratio <= 0.30:
            continue
        box = _clamp_box((x, y, x + bw, y + bh), w, h)
        if box is None:
            continue
        vertical_score = 1.0 if y / max(1, h) >= 0.20 else 0.65
        ratio_score = max(0.0, 1.0 - abs(ratio - 4.0) / 4.0)
        fill_score = min(1.0, cv2.contourArea(contour) / max(1, bw * bh))
        score = 0.50 * ratio_score + 0.30 * vertical_score + 0.20 * fill_score
        proposals.append((box, min(score, 0.75), "OPENCV", 0))

    proposals.sort(key=lambda item: item[1], reverse=True)
    selected = []
    for candidate in proposals:
        x1, y1, x2, y2 = candidate[0]
        duplicate = False
        for previous in selected:
            a1, b1, a2, b2 = previous[0]
            ix1, iy1 = max(x1, a1), max(y1, b1)
            ix2, iy2 = min(x2, a2), min(y2, b2)
            intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            area1 = (x2 - x1) * (y2 - y1)
            area2 = (a2 - a1) * (b2 - b1)
            if intersection / max(1, min(area1, area2)) > 0.85:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
        if len(selected) >= 5:
            break
    return selected


def _preprocess_variants(plate: np.ndarray):
    """Produce several OCR-friendly views without altering the saved original crop."""
    if not _valid_image(plate):
        return []
    gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY) if plate.ndim == 3 else plate.copy()
    h, w = gray.shape[:2]
    if h < 2 or w < 2:
        return []

    scale = min(5.0, max(1.0, 420.0 / max(1, w)))
    enlarged = cv2.resize(
        gray,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_LINEAR,
    )
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(enlarged)
    denoised = cv2.bilateralFilter(clahe, 7, 45, 45)
    sharpened = cv2.addWeighted(
        clahe, 1.5, cv2.GaussianBlur(clahe, (0, 0), 1.0), -0.5, 0
    )
    _, otsu = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 11,
    )
    return [
        ("gray", enlarged),
        ("clahe", clahe),
        ("denoised", denoised),
        ("sharpened", sharpened),
        ("otsu", otsu),
        ("otsu_inverted", cv2.bitwise_not(otsu)),
        ("adaptive", adaptive),
    ]


def _normalise_text(raw_text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw_text).upper())


def _is_plausible_registration(text: str) -> bool:
    """Broad validation: avoid assuming all registrations are South African."""
    if not 5 <= len(text) <= 10:
        return False
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    return letters >= 2 and digits >= 2


def _read_text(image: np.ndarray):
    """Return both raw EasyOCR readings and validated registration candidates."""
    if not _valid_image(image):
        return [], []
    h, w = image.shape[:2]
    if h < 8 or w < 20:
        return [], []

    try:
        results = reader.recognize(
            image,
            horizontal_list=[[0, w, 0, h]],
            free_list=[],
            detail=1,
            decoder="greedy",
            allowlist=ALLOWLIST,
            batch_size=1,
        )
    except Exception:
        logger.debug("EasyOCR failed on candidate crop", exc_info=True)
        return [], []

    raw_readings = []
    candidates = []
    for item in results or []:
        if len(item) < 3:
            continue
        raw = str(item[1]).strip()
        try:
            confidence = float(item[2])
        except (TypeError, ValueError):
            continue
        normalized = _normalise_text(raw)
        raw_readings.append({
            "raw_text": raw,
            "normalized": normalized,
            "confidence": round(confidence, 4),
        })
        if _is_plausible_registration(normalized):
            # Small preference for a balanced alphanumeric string, without
            # manufacturing or substituting characters in the OCR output.
            letters = sum(ch.isalpha() for ch in normalized)
            digits = sum(ch.isdigit() for ch in normalized)
            balance_bonus = 0.025 if letters >= 3 and digits >= 3 else 0.0
            candidates.append((normalized, min(1.0, confidence + balance_bonus), raw))

    candidates.sort(key=lambda item: item[1], reverse=True)
    return raw_readings, candidates


def detect_plate(vehicle_crop: np.ndarray, debug: bool = False):
    """Detect and read a plate inside a BGR vehicle crop.

    `bbox` remains relative to the supplied vehicle crop, matching the existing
    yolo_service integration. A detected crop is returned even when OCR fails,
    so the crop can be inspected and diagnosed instead of being discarded.
    """
    if not _valid_image(vehicle_crop):
        return None
    if vehicle_crop.ndim != 3 or vehicle_crop.shape[2] != 3:
        raise ValueError("Expected vehicle_crop as a 3-channel BGR image")
    if vehicle_crop.dtype != np.uint8:
        vehicle_crop = np.clip(vehicle_crop, 0, 255).astype(np.uint8)

    started = time.perf_counter()
    image_h, image_w = vehicle_crop.shape[:2]

    detections = _yolo_boxes(vehicle_crop, imgsz=640)
    used_size = 640
    if not detections:
        detections = _yolo_boxes(vehicle_crop, imgsz=960)
        used_size = 960
    if not detections:
        detections = _opencv_boxes(vehicle_crop)
        used_size = 0

    # Deduplicate proposals and retain a few for OCR-based ranking. Detector
    # confidence alone is not enough: a high-confidence square box can be wrong.
    unique = []
    seen = set()
    for box, det_conf, detector, imgsz in detections:
        box = _clamp_box(box, image_w, image_h)
        if box is None:
            continue
        key = tuple(round(v / 3) for v in box)
        if key in seen:
            continue
        seen.add(key)
        unique.append((box, det_conf, detector, imgsz))
    unique = unique[:6]

    best = None
    debug_candidates = []
    best_failed_crop = None
    best_failed_meta = None

    for (x1, y1, x2, y2), det_conf, detector, imgsz in unique:
        bw, bh = x2 - x1, y2 - y1
        px = max(2, round(bw * 0.05))
        py = max(2, round(bh * 0.12))
        left, top = max(0, x1 - px), max(0, y1 - py)
        right, bottom = min(image_w, x2 + px), min(image_h, y2 + py)
        crop = vehicle_crop[top:bottom, left:right].copy()
        if crop.size == 0:
            continue

        aspect_ratio = crop.shape[1] / max(1, crop.shape[0])
        if best_failed_crop is None or det_conf > best_failed_meta["detector_conf"]:
            best_failed_crop = crop
            best_failed_meta = {
                "bbox": (left, top, right, bottom),
                "detector_conf": float(det_conf),
                "detector": detector,
                "imgsz": imgsz or None,
                "aspect_ratio": round(aspect_ratio, 2),
            }

        variants = [("original", crop)] + _preprocess_variants(crop)
        for variant_name, variant in variants:
            raw_readings, candidates = _read_text(variant)
            if debug and raw_readings:
                debug_candidates.append({
                    "bbox": (left, top, right, bottom),
                    "variant": variant_name,
                    "readings": raw_readings,
                })
            for text, ocr_conf, raw_text in candidates:
                # OCR readability is the dominant signal; detector confidence
                # only breaks close ties between plate-shaped proposals.
                score = 0.90 * ocr_conf + 0.10 * float(det_conf)
                if best is None or score > best["score"]:
                    best = {
                        "text": text,
                        "raw_text": raw_text,
                        "ocr_conf": float(ocr_conf),
                        "detector_conf": float(det_conf),
                        "score": score,
                        "bbox": (left, top, right, bottom),
                        "crop": crop,
                        "imgsz": imgsz or None,
                        "detector": detector,
                        "variant": variant_name,
                        "aspect_ratio": round(aspect_ratio, 2),
                    }

    if best is not None and best["ocr_conf"] >= MIN_OCR_CONFIDENCE:
        status = "READ"
        registration = best["text"]
        selected_crop = best["crop"].copy()
        selected_bbox = best["bbox"]
        detector_conf = best["detector_conf"]
        ocr_conf = best["ocr_conf"]
        selected_imgsz = best["imgsz"]
        selected_detector = best["detector"]
        selected_variant = best["variant"]
        raw_text = best["raw_text"]
        aspect_ratio = best["aspect_ratio"]
    else:
        status = "OCR_FAILED" if best_failed_crop is not None else "NO_PLATE_BOX"
        registration = "UNKNOWN"
        selected_crop = best_failed_crop.copy() if best_failed_crop is not None else None
        selected_bbox = best_failed_meta["bbox"] if best_failed_meta else None
        detector_conf = best_failed_meta["detector_conf"] if best_failed_meta else 0.0
        ocr_conf = best["ocr_conf"] if best is not None else 0.0
        selected_imgsz = best_failed_meta["imgsz"] if best_failed_meta else used_size or None
        selected_detector = best_failed_meta["detector"] if best_failed_meta else None
        selected_variant = best["variant"] if best is not None else None
        raw_text = best["raw_text"] if best is not None else None
        aspect_ratio = best_failed_meta["aspect_ratio"] if best_failed_meta else None

    result = {
        "registration": registration,
        "confidence": round(float(detector_conf) * 100, 2),
        "ocr_confidence": round(float(ocr_conf) * 100, 2),
        "plate_crop": selected_crop,
        "bbox": selected_bbox,
        "status": status,
        "detection_imgsz": selected_imgsz,
        "processing_time": round(time.perf_counter() - started, 3),
        "detector": selected_detector,
        "ocr_variant": selected_variant,
        "raw_ocr_text": raw_text,
        "plate_aspect_ratio": aspect_ratio,
    }

    if debug:
        logger.info("[ANPR] result=%s", {k: v for k, v in result.items() if k != "plate_crop"})
        logger.info("[ANPR] raw OCR candidates=%s", debug_candidates)
        if selected_crop is not None:
            logger.info(
                "[ANPR] selected crop size=%sx%s",
                selected_crop.shape[1], selected_crop.shape[0],
            )
    return result


def _main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Test OverWatch AI ANPR on a vehicle crop/image")
    parser.add_argument("image", help="Path to a vehicle image")
    parser.add_argument("--save-crop", help="Optional path to save the selected plate crop")
    parser.add_argument("--debug", action="store_true", help="Log raw OCR candidates")
    args = parser.parse_args()

    image = cv2.imread(args.image)
    if image is None:
        raise SystemExit(f"Could not read image: {args.image}")
    result = detect_plate(image, debug=args.debug)
    if result is None:
        print("No valid input image/crop")
        raise SystemExit(2)

    printable = {k: v for k, v in result.items() if k != "plate_crop"}
    print("[ANPR RESULT]", printable)
    if args.save_crop and result.get("plate_crop") is not None:
        output = Path(args.save_crop)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(output), result["plate_crop"]):
            raise SystemExit(f"Could not save crop to {output}")
        print(f"[ANPR] Plate crop saved to: {output}")


if __name__ == "__main__":
    _main()
