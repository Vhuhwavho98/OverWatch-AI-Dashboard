"""
=========================================================
OverWatch AI - Intelligent Overload Detection Service
YOLOv11 + Computer Vision Heuristics
=========================================================

This service estimates overload severity from vehicle
dimensions detected by YOLO.

Outputs
-------
SAFE
WARNING
HIGH

Severity Score
--------------
0 - 39   SAFE
40 - 69  WARNING
70 -100  HIGH
"""

import cv2
import numpy as np

# ==========================================================
# VEHICLE REFERENCE DIMENSIONS
# Width : Height ratios for common vehicle classes.
# ==========================================================

REFERENCE_RATIO = {
    "Car": 1.75,
    "Truck": 2.60,
    "Bus": 2.45,
    "Van": 2.10,
    "Pickup": 2.25,
    "Motorcycle": 0.90,
    "Minibus": 2.15,
}

# Expected image occupancy (rough estimate)
EXPECTED_OCCUPANCY = {
    "Car": 0.22,
    "Truck": 0.30,
    "Bus": 0.33,
    "Van": 0.26,
    "Pickup": 0.27,
    "Motorcycle": 0.12,
    "Minibus": 0.28,
}

# ==========================================================
# IMAGE QUALITY
# ==========================================================

def image_quality(image):
    """
    Returns Laplacian sharpness score.
    """

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    score = cv2.Laplacian(gray, cv2.CV_64F).var()

    return score


# ==========================================================
# EDGE DENSITY
# ==========================================================

def edge_density(image):
    """
    Estimates how visually dense the cargo/body is.
    """

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    edges = cv2.Canny(gray, 70, 180)

    density = np.sum(edges > 0) / edges.size

    return density


# ==========================================================
# BRIGHTNESS
# ==========================================================

def brightness(image):

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    return hsv[:, :, 2].mean()


# ==========================================================
# VEHICLE FEATURES
# ==========================================================

def vehicle_features(vehicle_crop):

    h, w = vehicle_crop.shape[:2]

    area = h * w

    aspect_ratio = w / max(h, 1)

    occupancy = area / (640 * 640)

    edges = edge_density(vehicle_crop)

    sharpness = image_quality(vehicle_crop)

    light = brightness(vehicle_crop)

    return {
        "width": w,
        "height": h,
        "area": area,
        "aspect_ratio": aspect_ratio,
        "occupancy": occupancy,
        "edge_density": edges,
        "sharpness": sharpness,
        "brightness": light,
    }


# ==========================================================
# OVERLOAD SCORING
# ==========================================================

def overload_score(vehicle_type, features):

    score = 0

    expected_ratio = REFERENCE_RATIO.get(vehicle_type, 2.0)

    expected_area = EXPECTED_OCCUPANCY.get(vehicle_type, 0.25)

    ratio_difference = features["aspect_ratio"] / expected_ratio

    # Vehicle looks wider/taller than expected.
    if ratio_difference > 1.15:
        score += 25

    elif ratio_difference > 1.05:
        score += 15

    # Occupancy inside image.
    if features["occupancy"] > expected_area * 1.30:
        score += 30

    elif features["occupancy"] > expected_area * 1.10:
        score += 20

    # Dense cargo/body.
    if features["edge_density"] > 0.16:
        score += 18

    elif features["edge_density"] > 0.12:
        score += 10

    # Very dark images reduce confidence.
    if features["brightness"] < 45:
        score -= 5

    # Blurry image penalty.
    if features["sharpness"] < 60:
        score -= 5

    return max(0, min(100, round(score)))


# ==========================================================
# CLASSIFICATION
# ==========================================================

def classify_overload(vehicle_type, vehicle_crop):
    """
    Returns overload status and severity score.

    Returns
    -------
    status : SAFE / WARNING / HIGH
    score : 0-100
    """

    if vehicle_crop is None or vehicle_crop.size == 0:
        return "SAFE", 0

    features = vehicle_features(vehicle_crop)

    score = overload_score(vehicle_type, features)

    if score >= 70:
        status = "HIGH"

    elif score >= 40:
        status = "WARNING"

    else:
        status = "SAFE"

    return status, score


# ==========================================================
# DEBUG INFORMATION
# ==========================================================

def explain_overload(vehicle_type, vehicle_crop):
    """
    Useful for dashboard analytics and IEEE evaluation.
    """

    features = vehicle_features(vehicle_crop)

    status, score = classify_overload(vehicle_type, vehicle_crop)

    return {
        "vehicle_type": vehicle_type,
        "status": status,
        "score": score,
        "features": features,
    }


# ==========================================================
# TEST
# ==========================================================

if __name__ == "__main__":

    image = cv2.imread("test_vehicle.jpg")

    status, score = classify_overload("Truck", image)

    print(status)
    print(score)