"""OverWatch AI - efficient plate localization, enhancement and OCR.

Input: BGR uint8 NumPy array containing one vehicle crop.
A supplied plate_bbox (crop-relative xyxy) is preferred so the plate detector
is not run a second time. Otherwise the dedicated plate detector runs once.
Models/readers are loaded once per process.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional, Sequence

import cv2
import easyocr
import numpy as np
import torch
from huggingface_hub import hf_hub_download
from ultralytics import YOLO

logger = logging.getLogger(__name__)

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
USE_HALF = DEVICE.startswith("cuda")
ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
PLATE_MODEL_REPO = "morsetechlab/yolov11-license-plate-detection"
PLATE_MODEL_FILE = "license-plate-finetune-v1n.pt"
OCR_ACCEPT_CONFIDENCE = 0.65
OCR_MIN_CONFIDENCE = 0.45

WEIGHTS = hf_hub_download(repo_id=PLATE_MODEL_REPO, filename=PLATE_MODEL_FILE)
plate_model = YOLO(WEIGHTS)
if DEVICE.startswith("cuda"):
    plate_model.to(DEVICE)

reader = easyocr.Reader(
    ["en"], gpu=torch.cuda.is_available(), detector=False,
    recognizer=True, verbose=False,
)


def _clean_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _locate_plate(image: np.ndarray):
    """One dedicated-detector pass; return up to 3 boxes in crop coordinates."""
    try:
        with torch.inference_mode():
            result = plate_model.predict(
                source=image, imgsz=640, conf=0.12, iou=0.45,
                max_det=3, device=DEVICE, half=USE_HALF, verbose=False,
            )[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        found = []
        for box in boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0].item())
            if x2 > x1 and y2 > y1:
                found.append(((x1, y1, x2, y2), conf, "dedicated_plate_model"))
        return sorted(found, key=lambda item: item[1], reverse=True)[:3]
    except Exception:
        logger.exception("Dedicated plate localization failed")
        return []


def _ocr(image: np.ndarray) -> tuple[str, float]:
    if image is None or image.size == 0:
        return "", 0.0
    h, w = image.shape[:2]
    if h < 8 or w < 20:
        return "", 0.0
    try:
        found = reader.recognize(
            image, horizontal_list=[[0, w, 0, h]], free_list=[],
            detail=1, decoder="greedy", allowlist=ALLOWLIST, batch_size=1,
        )
    except Exception:
        logger.debug("EasyOCR recognition failed", exc_info=True)
        return "", 0.0
    candidates = []
    for item in found or []:
        if len(item) < 3:
            continue
        text = _clean_text(item[1])
        try:
            confidence = float(item[2])
        except (TypeError, ValueError):
            continue
        if 4 <= len(text) <= 10 and any(ch.isdigit() for ch in text):
            candidates.append((text, confidence))
    return max(candidates, key=lambda item: item[1]) if candidates else ("", 0.0)


def _ocr_variants(plate: np.ndarray):
    """Yield original first, then a small set of cheap enhancement variants."""
    if plate.ndim == 2:
        gray = plate.copy()
    else:
        gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)

    h, w = gray.shape[:2]
    if h < 2 or w < 2:
        return
    # Upscale small crops, capped to avoid expensive huge tensors.
    target_w = min(640, max(240, w))
    scale = target_w / float(w)
    if scale > 1.0:
        gray = cv2.resize(
            gray, (target_w, max(24, int(round(h * scale)))),
            interpolation=cv2.INTER_CUBIC,
        )

    yield "grayscale_upscaled", gray

    # CLAHE and mild sharpening are only reached if the original OCR is weak.
    denoised = cv2.bilateralFilter(gray, 5, 35, 35)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    yield "clahe", enhanced

    blurred = cv2.GaussianBlur(enhanced, (0, 0), 1.0)
    sharpened = cv2.addWeighted(enhanced, 1.5, blurred, -0.5, 0)
    yield "sharpened", sharpened

    _, thresholded = cv2.threshold(
        enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    yield "otsu", thresholded


def _normalise_bbox(plate_bbox, image_w: int, image_h: int):
    if plate_bbox is None:
        return None
    try:
        if hasattr(plate_bbox, "tolist"):
            plate_bbox = plate_bbox.tolist()
        if len(plate_bbox) == 1 and hasattr(plate_bbox[0], "__len__"):
            plate_bbox = plate_bbox[0]
        x1, y1, x2, y2 = [int(round(float(v))) for v in plate_bbox[:4]]
        x1 = max(0, min(x1, image_w - 1))
        y1 = max(0, min(y1, image_h - 1))
        x2 = max(0, min(x2, image_w))
        y2 = max(0, min(y2, image_h))
        return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None
    except (TypeError, ValueError, IndexError):
        logger.warning("Ignoring invalid crop-relative plate_bbox=%r", plate_bbox)
        return None


def detect_plate(
    vehicle_crop: np.ndarray,
    debug: bool = False,
    plate_bbox=None,
    plate_detection_confidence: float = 0.0,
):
    """Recognize a plate; accepts optional crop-relative xyxy from YOLO26.

    If plate_bbox is supplied, no additional plate-localization model call is
    made. If absent, one dedicated plate-detector pass is performed.
    """
    started = time.perf_counter()
    if not isinstance(vehicle_crop, np.ndarray) or vehicle_crop.size == 0:
        return None
    if vehicle_crop.ndim != 3 or vehicle_crop.shape[2] != 3:
        raise ValueError("Expected vehicle_crop as a 3-channel BGR image")

    image_h, image_w = vehicle_crop.shape[:2]
    supplied_box = _normalise_bbox(plate_bbox, image_w, image_h)
    if supplied_box is not None:
        try:
            det_conf = float(plate_detection_confidence or 0.0)
        except (TypeError, ValueError):
            det_conf = 0.0
        detections = [(supplied_box, det_conf, "yolo26_candidate")]
    else:
        detections = _locate_plate(vehicle_crop)

    if not detections:
        return {
            "registration": "OCR NOT READABLE", "text": "",
            "confidence": 0.0, "ocr_confidence": 0.0,
            "plate_crop": None, "bbox": None, "status": "NO_PLATE_BOX",
            "plate_box_source": "no_detection",
            "processing_time": round(time.perf_counter() - started, 3),
        }

    best = None
    for (x1, y1, x2, y2), detector_conf, source in detections[:3]:
        # A small margin prevents clipping characters, especially at plate edges.
        pad_x = max(2, int(round((x2 - x1) * 0.08)))
        pad_y = max(2, int(round((y2 - y1) * 0.14)))
        left, top = max(0, x1 - pad_x), max(0, y1 - pad_y)
        right, bottom = min(image_w, x2 + pad_x), min(image_h, y2 + pad_y)
        original_crop = vehicle_crop[top:bottom, left:right].copy()
        if original_crop.size == 0:
            continue

        # First OCR attempt on the unmodified colour crop.
        text, ocr_conf = _ocr(original_crop)
        best_variant = "original"
        if debug:
            logger.info("[PLATE OCR] source=%s variant=original text=%s conf=%.3f",
                        source, text or "<none>", ocr_conf)

        # Only spend extra OCR calls when the raw crop is not convincing.
        if not text or ocr_conf < OCR_ACCEPT_CONFIDENCE:
            for variant_name, variant in _ocr_variants(original_crop):
                candidate, candidate_conf = _ocr(variant)
                if debug:
                    logger.info("[PLATE OCR] source=%s variant=%s text=%s conf=%.3f",
                                source, variant_name, candidate or "<none>", candidate_conf)
                if candidate and candidate_conf > ocr_conf:
                    text, ocr_conf, best_variant = candidate, candidate_conf, variant_name
                if ocr_conf >= OCR_ACCEPT_CONFIDENCE:
                    break

        score = 0.90 * ocr_conf + 0.10 * float(detector_conf)
        if text and (best is None or score > best["score"]):
            best = {
                "text": text, "ocr_conf": float(ocr_conf),
                "detector_conf": float(detector_conf), "score": score,
                "bbox": (left, top, right, bottom),
                "crop": original_crop, "variant": best_variant,
                "source": source,
            }
        # A strong result from the highest-ranked plate box is sufficient.
        if best is not None and best["ocr_conf"] >= OCR_ACCEPT_CONFIDENCE:
            break

    if best is None:
        detector_conf = max((d[1] for d in detections), default=0.0)
        data = {
            "registration": "OCR NOT READABLE", "text": "",
            "confidence": round(detector_conf * 100, 2),
            "ocr_confidence": 0.0, "plate_crop": None, "bbox": None,
            "status": "OCR_FAILED", "plate_box_source": detections[0][2],
        }
    else:
        status = "READ" if best["ocr_conf"] >= OCR_MIN_CONFIDENCE else "LOW_OCR_CONFIDENCE"
        data = {
            "registration": best["text"] if status == "READ" else "OCR NOT READABLE",
            "text": best["text"],
            "confidence": round(best["detector_conf"] * 100, 2),
            "ocr_confidence": round(best["ocr_conf"] * 100, 2),
            "plate_crop": best["crop"], "bbox": best["bbox"],
            "status": status, "plate_box_source": best["source"],
            "preprocessing_variant": best["variant"],
        }

    data["processing_time"] = round(time.perf_counter() - started, 3)
    if debug:
        logger.info("[PLATE OCR] result=%s", {k: v for k, v in data.items() if k != "plate_crop"})
    return data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test ANPR on a vehicle image")
    parser.add_argument("image", help="Path to an image containing a vehicle")
    args = parser.parse_args()
    frame = cv2.imread(args.image)
    if frame is None:
        raise SystemExit(f"Could not read image: {args.image}")
    logging.basicConfig(level=logging.INFO)
    print(detect_plate(frame, debug=True))
