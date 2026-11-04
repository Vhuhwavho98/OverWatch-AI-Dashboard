# ============================================================
# OVERWATCH AI - ANPR SERVICE
# ============================================================
#
# Vehicle crop
#     ↓
# Plate detection
#     ↓
# YOLO plate model if available
#     ↓
# OpenCV fallback if model unavailable
#     ↓
# EasyOCR
#     ↓
# Registration number
#
# IMPORTANT:
# This file MUST NOT import yolo_service.
# This file MUST NOT import itself.
# ============================================================

from pathlib import Path
import os
import re
import urllib.request

import cv2
import numpy as np

try:
    import easyocr
except ImportError:
    easyocr = None

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

MODELS_DIR = BASE_DIR / "models"

MODELS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

PLATE_MODEL_PATH = (
    MODELS_DIR
    / "license_plate_detector.pt"
)


# ============================================================
# OPTIONAL AUTOMATIC DOWNLOAD
# ============================================================

# Set PLATE_MODEL_URL as an environment variable if you have
# a trusted direct-download URL for your trained plate model.
#
# If it is empty, OpenCV fallback will be used.

PLATE_MODEL_URL = os.getenv(
    "PLATE_MODEL_URL",
    "",
)


# ============================================================
# SETTINGS
# ============================================================

PLATE_CONFIDENCE_THRESHOLD = 0.30

OCR_CONFIDENCE_THRESHOLD = 0.20

MIN_PLATE_WIDTH = 35

MIN_PLATE_HEIGHT = 10


# ============================================================
# DOWNLOAD PLATE MODEL
# ============================================================

def ensure_plate_model():

    if PLATE_MODEL_PATH.exists():

        print(
            "ANPR model found:",
            PLATE_MODEL_PATH,
        )

        return True

    if not PLATE_MODEL_URL:

        print()
        print("=" * 60)
        print("ANPR MODEL NOT INSTALLED")
        print("Expected model:")
        print(PLATE_MODEL_PATH)
        print()
        print(
            "PLATE_MODEL_URL has not been configured."
        )
        print(
            "OpenCV plate detector will be used."
        )
        print("=" * 60)
        print()

        return False

    temporary_path = (
        MODELS_DIR
        / "license_plate_detector.download"
    )

    try:

        print(
            "Downloading ANPR model..."
        )

        urllib.request.urlretrieve(
            PLATE_MODEL_URL,
            temporary_path,
        )

        if not temporary_path.exists():

            raise RuntimeError(
                "Model download failed."
            )

        file_size = (
            temporary_path.stat().st_size
        )

        if file_size < 100_000:

            raise RuntimeError(
                "Downloaded file is too small "
                "to be a valid YOLO model."
            )

        temporary_path.replace(
            PLATE_MODEL_PATH
        )

        print(
            "ANPR model downloaded:",
            PLATE_MODEL_PATH,
        )

        return True

    except Exception as error:

        print(
            "ANPR MODEL DOWNLOAD ERROR:",
            error,
        )

        if temporary_path.exists():

            try:
                temporary_path.unlink()
            except Exception:
                pass

        return False


# ============================================================
# LOAD OPTIONAL PLATE MODEL
# ============================================================

plate_model = None

ensure_plate_model()

if (
    PLATE_MODEL_PATH.exists()
    and YOLO is not None
):

    try:

        print(
            "Loading ANPR plate model..."
        )

        plate_model = YOLO(
            str(PLATE_MODEL_PATH)
        )

        print(
            "ANPR YOLO plate detector ready."
        )

    except Exception as error:

        print(
            "ANPR MODEL LOAD ERROR:",
            error,
        )

        plate_model = None

else:

    print(
        "Dedicated ANPR YOLO detector unavailable."
    )

    print(
        "Using OpenCV plate detector fallback."
    )


# ============================================================
# LOAD EASYOCR
# ============================================================

ocr_reader = None

if easyocr is not None:

    try:

        print(
            "Loading EasyOCR ANPR engine..."
        )

        ocr_reader = easyocr.Reader(
            ["en"],
            gpu=False,
        )

        print(
            "EasyOCR ANPR engine ready."
        )

    except Exception as error:

        print(
            "EasyOCR initialization error:",
            error,
        )

else:

    print(
        "EasyOCR is not installed."
    )

    print(
        "Install using: pip install easyocr"
    )


# ============================================================
# IMAGE VALIDATION
# ============================================================

def valid_image(image):

    return (
        image is not None
        and isinstance(image, np.ndarray)
        and image.size > 0
    )


# ============================================================
# CLEAN REGISTRATION
# ============================================================

def clean_registration(text):

    if not text:

        return "UNKNOWN"

    text = str(text).upper().strip()

    text = re.sub(
        r"[^A-Z0-9\-\s]",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


# ============================================================
# NORMALIZE REGISTRATION
# ============================================================

def normalize_registration(text):

    if not text:

        return ""

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(text).upper(),
    )


# ============================================================
# BASIC REGISTRATION VALIDATION
# ============================================================

def valid_registration(text):

    value = normalize_registration(
        text
    )

    if len(value) < 4:
        return False

    if len(value) > 12:
        return False

    return True


# ============================================================
# YOLO PLATE DETECTOR
# ============================================================

def detect_plate_yolo(vehicle_crop):

    if plate_model is None:

        return None

    if not valid_image(
        vehicle_crop
    ):

        return None

    try:

        results = plate_model.predict(
            vehicle_crop,
            conf=PLATE_CONFIDENCE_THRESHOLD,
            imgsz=640,
            verbose=False,
        )

    except Exception as error:

        print(
            "ANPR YOLO ERROR:",
            error,
        )

        return None

    best = None

    best_confidence = 0.0

    for result in results:

        if result.boxes is None:
            continue

        for box in result.boxes:

            confidence = float(
                box.conf[0]
            )

            if confidence <= best_confidence:
                continue

            x1, y1, x2, y2 = map(
                int,
                box.xyxy[0],
            )

            width = x2 - x1

            height = y2 - y1

            if width < MIN_PLATE_WIDTH:
                continue

            if height < MIN_PLATE_HEIGHT:
                continue

            best_confidence = confidence

            best = {

                "bbox": (
                    x1,
                    y1,
                    x2,
                    y2,
                ),

                "confidence":
                    confidence,

                "detector":
                    "YOLO",

            }

    return best


# ============================================================
# OPENCV FALLBACK PLATE DETECTOR
# ============================================================

def detect_plate_opencv(vehicle_crop):

    if not valid_image(
        vehicle_crop
    ):

        return None

    image_height, image_width = (
        vehicle_crop.shape[:2]
    )

    gray = cv2.cvtColor(
        vehicle_crop,
        cv2.COLOR_BGR2GRAY,
    )

    clahe = cv2.createCLAHE(
        clipLimit=2.0,
        tileGridSize=(8, 8),
    )

    gray = clahe.apply(
        gray
    )

    blurred = cv2.bilateralFilter(
        gray,
        11,
        17,
        17,
    )

    edges = cv2.Canny(
        blurred,
        40,
        180,
    )

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (5, 3),
    )

    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        kernel,
    )

    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    candidates = []

    vehicle_area = max(
        image_width * image_height,
        1,
    )

    for contour in contours:

        x, y, w, h = (
            cv2.boundingRect(
                contour
            )
        )

        if w < MIN_PLATE_WIDTH:
            continue

        if h < MIN_PLATE_HEIGHT:
            continue

        aspect_ratio = (
            w / max(h, 1)
        )

        if not (
            1.8
            <= aspect_ratio
            <= 7.5
        ):
            continue

        area_ratio = (
            (w * h)
            / vehicle_area
        )

        if not (
            0.002
            <= area_ratio
            <= 0.30
        ):
            continue

        vertical_position = (
            y
            / max(
                image_height,
                1,
            )
        )

        aspect_score = max(
            0.0,
            1.0
            - (
                abs(
                    aspect_ratio - 4.0
                )
                / 4.0
            ),
        )

        if vertical_position >= 0.30:

            position_score = 1.0

        else:

            position_score = 0.5

        rectangular_area = max(
            w * h,
            1,
        )

        contour_area = (
            cv2.contourArea(
                contour
            )
        )

        fill_score = min(
            contour_area
            / rectangular_area,
            1.0,
        )

        score = (
            0.45 * aspect_score
            + 0.35 * position_score
            + 0.20 * fill_score
        )

        candidates.append(
            {

                "bbox": (
                    x,
                    y,
                    x + w,
                    y + h,
                ),

                "confidence":
                    min(
                        float(score),
                        0.75,
                    ),

                "detector":
                    "OPENCV",

            }
        )

    if not candidates:

        return None

    candidates.sort(
        key=lambda item:
            item["confidence"],
        reverse=True,
    )

    return candidates[0]


# ============================================================
# DETECT PLATE REGION
# ============================================================

def detect_plate_region(vehicle_crop):

    # First use dedicated YOLO plate model.

    result = detect_plate_yolo(
        vehicle_crop
    )

    if result is not None:

        return result

    # Fall back to OpenCV.

    return detect_plate_opencv(
        vehicle_crop
    )


# ============================================================
# PREPROCESS PLATE
# ============================================================

def preprocess_plate(plate_crop):

    if not valid_image(
        plate_crop
    ):

        return []

    if len(plate_crop.shape) == 3:

        gray = cv2.cvtColor(
            plate_crop,
            cv2.COLOR_BGR2GRAY,
        )

    else:

        gray = plate_crop.copy()

    # Upscale plate.

    enlarged = cv2.resize(
        gray,
        None,
        fx=3,
        fy=3,
        interpolation=cv2.INTER_CUBIC,
    )

    clahe = cv2.createCLAHE(
        clipLimit=2.0,
        tileGridSize=(8, 8),
    )

    enhanced = clahe.apply(
        enlarged
    )

    denoised = cv2.bilateralFilter(
        enhanced,
        9,
        75,
        75,
    )

    _, threshold = cv2.threshold(
        denoised,
        0,
        255,
        (
            cv2.THRESH_BINARY
            + cv2.THRESH_OTSU
        ),
    )

    inverted = cv2.bitwise_not(
        threshold
    )

    adaptive = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )

    return [

        enlarged,

        enhanced,

        denoised,

        threshold,

        inverted,

        adaptive,

    ]


# ============================================================
# OCR
# ============================================================

def run_ocr(image):

    if ocr_reader is None:

        return None

    if not valid_image(
        image
    ):

        return None

    try:

        results = ocr_reader.readtext(
            image,
            detail=1,
            paragraph=False,
            allowlist=(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789- "
            ),
        )

    except Exception as error:

        print(
            "ANPR OCR ERROR:",
            error,
        )

        return None

    candidates = []

    for result in results:

        if len(result) < 3:
            continue

        _, text, confidence = result

        confidence = float(
            confidence
        )

        if (
            confidence
            < OCR_CONFIDENCE_THRESHOLD
        ):
            continue

        registration = (
            clean_registration(
                text
            )
        )

        if not valid_registration(
            registration
        ):
            continue

        candidates.append(
            {

                "registration":
                    registration,

                "confidence":
                    confidence,

            }
        )

    if not candidates:

        return None

    candidates.sort(
        key=lambda item:
            item["confidence"],
        reverse=True,
    )

    return candidates[0]


# ============================================================
# READ PLATE
# ============================================================

def read_plate(plate_crop):

    images = preprocess_plate(
        plate_crop
    )

    candidates = []

    for image in images:

        result = run_ocr(
            image
        )

        if result:

            candidates.append(
                result
            )

    if not candidates:

        return None

    grouped = {}

    for candidate in candidates:

        registration = (
            candidate[
                "registration"
            ]
        )

        normalized = (
            normalize_registration(
                registration
            )
        )

        if normalized not in grouped:

            grouped[
                normalized
            ] = {

                "registration":
                    registration,

                "confidence":
                    candidate[
                        "confidence"
                    ],

                "count":
                    1,

            }

        else:

            grouped[
                normalized
            ][
                "count"
            ] += 1

            grouped[
                normalized
            ][
                "confidence"
            ] = max(

                grouped[
                    normalized
                ][
                    "confidence"
                ],

                candidate[
                    "confidence"
                ],

            )

    best = max(

        grouped.values(),

        key=lambda item: (

            item["count"],

            item["confidence"],

        ),
    )

    return {

        "registration":
            best[
                "registration"
            ],

        "confidence":
            float(
                best[
                    "confidence"
                ]
            ),

    }


# ============================================================
# MAIN ANPR FUNCTION
# ============================================================

def recognize_plate(vehicle_crop):
    """
    Main function used by yolo_service.py.
    """

    if not valid_image(
        vehicle_crop
    ):

        return None

    # --------------------------------------------------------
    # Detect plate
    # --------------------------------------------------------

    detection = detect_plate_region(
        vehicle_crop
    )

    if detection is None:

        return None

    image_height, image_width = (
        vehicle_crop.shape[:2]
    )

    x1, y1, x2, y2 = (
        detection["bbox"]
    )

    x1 = max(
        0,
        min(
            int(x1),
            image_width - 1,
        ),
    )

    y1 = max(
        0,
        min(
            int(y1),
            image_height - 1,
        ),
    )

    x2 = max(
        x1 + 1,
        min(
            int(x2),
            image_width,
        ),
    )

    y2 = max(
        y1 + 1,
        min(
            int(y2),
            image_height,
        ),
    )

    # --------------------------------------------------------
    # Add margin
    # --------------------------------------------------------

    width = x2 - x1

    height = y2 - y1

    margin_x = int(
        width * 0.05
    )

    margin_y = int(
        height * 0.10
    )

    crop_x1 = max(
        x1 - margin_x,
        0,
    )

    crop_y1 = max(
        y1 - margin_y,
        0,
    )

    crop_x2 = min(
        x2 + margin_x,
        image_width,
    )

    crop_y2 = min(
        y2 + margin_y,
        image_height,
    )

    plate_crop = vehicle_crop[
        crop_y1:crop_y2,
        crop_x1:crop_x2
    ].copy()

    if not valid_image(
        plate_crop
    ):

        return None

    # --------------------------------------------------------
    # OCR
    # --------------------------------------------------------

    ocr_result = read_plate(
        plate_crop
    )

    if ocr_result is None:

        return {

            "registration":
                "UNKNOWN",

            "detection_confidence":
                float(
                    detection[
                        "confidence"
                    ]
                ),

            "ocr_confidence":
                0.0,

            "bbox": (
                x1,
                y1,
                x2,
                y2,
            ),

            "detector":
                detection[
                    "detector"
                ],

        }

    registration = clean_registration(
        ocr_result[
            "registration"
        ]
    )

    return {

        "registration":
            registration,

        "detection_confidence":
            float(
                detection[
                    "confidence"
                ]
            ),

        "ocr_confidence":
            float(
                ocr_result[
                    "confidence"
                ]
            ),

        "bbox": (
            x1,
            y1,
            x2,
            y2,
        ),

        "detector":
            detection[
                "detector"
            ],

    }


# ============================================================
# BACKWARD-COMPATIBILITY FUNCTION
# ============================================================

def detect_plate(vehicle_crop):

    result = recognize_plate(
        vehicle_crop
    )

    if result is None:

        return None

    return {

        "registration":
            result.get(
                "registration",
                "UNKNOWN",
            ),

        "confidence":
            result.get(
                "ocr_confidence",
                0.0,
            ),

        "bbox":
            result.get(
                "bbox"
            ),

    }