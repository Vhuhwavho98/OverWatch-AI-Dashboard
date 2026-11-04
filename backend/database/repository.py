# ==========================================================
# OverWatch AI - SQL Server Repository
# Handles CRUD and live vehicle-record updates for OverloadDB
# Compatible with the supplied database/models.py
# ==========================================================

from pathlib import Path
from datetime import datetime, timedelta

from sqlalchemy import desc
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import Detection


UNKNOWN_REGISTRATIONS = {
    "",
    "UNKNOWN",
    "OCR NOT READABLE",
    "NEEDS REVIEW",
    "N/A",
    "NONE",
}


def _clean_registration(value):
    """Return a normalised registration, or None for an unreadable result."""
    if value is None:
        return None

    cleaned = str(value).strip().upper()
    cleaned = " ".join(cleaned.split())

    if cleaned in UNKNOWN_REGISTRATIONS:
        return None

    return cleaned


def format_snapshot(snapshot):
    """
    Convert a stored snapshot path into a URL served by FastAPI.

    The backend must mount its uploads directory, for example:
        app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
    """
    if not snapshot:
        return None

    value = str(snapshot).replace("\\", "/")

    if value.startswith(("http://", "https://")):
        return value

    if value.startswith("/uploads/"):
        return value

    marker = "uploads/"
    if marker in value:
        return "/" + marker + value.split(marker, 1)[1].lstrip("/")

    return f"/uploads/snapshots/{Path(value).name}"


def _apply_optional_fields(
    detection,
    *,
    vehicle_type=None,
    confidence=None,
    overload_status=None,
    severity_score=None,
    location=None,
    snapshot=None,
    latitude=None,
    longitude=None,
):
    """Update only fields for which a value was supplied."""
    values = {
        "vehicle_type": vehicle_type,
        "confidence": confidence,
        "overload_status": overload_status,
        "severity_score": severity_score,
        "location": location,
        "snapshot": snapshot,
        "latitude": latitude,
        "longitude": longitude,
    }

    for field, value in values.items():
        if value is not None:
            setattr(detection, field, value)


# ==========================================================
# CREATE DETECTION
# ==========================================================

def save_detection(
    tracker_id: int,
    vehicle_type: str,
    registration_number: str = "UNKNOWN",
    confidence: float = 0.0,
    overload_status: str = "INDETERMINATE",
    severity_score: int = 0,
    location: str = "Unknown",
    snapshot: str = None,
    latitude: float = None,
    longitude: float = None,
):
    """Insert one vehicle detection into SQL Server."""
    db: Session = SessionLocal()

    try:
        registration = _clean_registration(registration_number) or "UNKNOWN"

        detection = Detection(
            tracker_id=int(tracker_id),
            vehicle_type=vehicle_type,
            registration_number=registration,
            confidence=float(confidence or 0.0),
            overload_status=overload_status or "INDETERMINATE",
            severity_score=int(severity_score or 0),
            location=location,
            snapshot=snapshot,
            latitude=latitude,
            longitude=longitude,
        )

        db.add(detection)
        db.commit()
        db.refresh(detection)
        return detection

    except Exception as exc:
        db.rollback()
        print(f"[DATABASE] Save detection failed: {exc}")
        raise

    finally:
        db.close()


# ==========================================================
# UPDATE EXISTING VEHICLE BY TRACKER ID
# ==========================================================

def update_detection_by_tracker_id(
    tracker_id: int,
    registration_number: str = None,
    vehicle_type: str = None,
    confidence: float = None,
    overload_status: str = None,
    severity_score: int = None,
    location: str = None,
    snapshot: str = None,
    latitude: float = None,
    longitude: float = None,
    max_age_minutes: int = 5,
):
    """
    Update the newest recent record for this tracker ID and location.

    A recognised ANPR result updates registration_number. An UNKNOWN or
    empty OCR result never overwrites a registration already recognised.

    The five-minute limit reduces accidental updates to old records if
    ByteTrack reuses an ID after a restart. For robust multi-camera or
    long-running deployments, add a camera/session ID to the database key.
    """
    db: Session = SessionLocal()

    try:
        query = db.query(Detection).filter(
            Detection.tracker_id == int(tracker_id)
        )

        if location is not None:
            query = query.filter(Detection.location == location)

        # SQL Server DateTime columns commonly return naive datetimes.
        # Use a naive UTC cutoff to avoid aware/naive comparison issues.
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        query = query.filter(Detection.timestamp >= cutoff)

        detection = query.order_by(desc(Detection.timestamp)).first()

        if detection is None:
            return None

        registration = _clean_registration(registration_number)
        if registration:
            detection.registration_number = registration

        _apply_optional_fields(
            detection,
            vehicle_type=vehicle_type,
            confidence=confidence,
            overload_status=overload_status,
            severity_score=severity_score,
            location=location,
            snapshot=snapshot,
            latitude=latitude,
            longitude=longitude,
        )

        db.commit()
        db.refresh(detection)
        return detection

    except Exception as exc:
        db.rollback()
        print(f"[DATABASE] Update detection failed: {exc}")
        raise

    finally:
        db.close()


# ==========================================================
# CREATE OR UPDATE VEHICLE RECORD
# ==========================================================

def save_or_update_detection(
    tracker_id: int,
    vehicle_type: str,
    registration_number: str = "UNKNOWN",
    confidence: float = 0.0,
    overload_status: str = "INDETERMINATE",
    severity_score: int = 0,
    location: str = "Unknown",
    snapshot: str = None,
    latitude: float = None,
    longitude: float = None,
):
    """
    Save the first observation for a tracker, then update that record.

    Call this from the live detection pipeline:
      - First sighting: creates a row, even if registration is UNKNOWN.
      - Later ANPR success: updates the same recent row with the plate.
      - Later UNKNOWN OCR: preserves any previously recognised plate.
    """
    existing = update_detection_by_tracker_id(
        tracker_id=tracker_id,
        registration_number=registration_number,
        vehicle_type=vehicle_type,
        confidence=confidence,
        overload_status=overload_status,
        severity_score=severity_score,
        location=location,
        snapshot=snapshot,
        latitude=latitude,
        longitude=longitude,
    )

    if existing is not None:
        return existing

    return save_detection(
        tracker_id=tracker_id,
        vehicle_type=vehicle_type,
        registration_number=registration_number,
        confidence=confidence,
        overload_status=overload_status,
        severity_score=severity_score,
        location=location,
        snapshot=snapshot,
        latitude=latitude,
        longitude=longitude,
    )


# ==========================================================
# GET ALL DETECTIONS
# ==========================================================

def get_all_detections():
    db: Session = SessionLocal()

    try:
        return (
            db.query(Detection)
            .order_by(desc(Detection.timestamp))
            .all()
        )
    finally:
        db.close()


# ==========================================================
# GET LATEST DETECTION
# ==========================================================

def get_latest_detection():
    db: Session = SessionLocal()

    try:
        latest = (
            db.query(Detection)
            .order_by(desc(Detection.timestamp))
            .first()
        )

        if latest is None:
            return {"vehicles": []}

        return {
            "vehicles": [
                {
                    "id": latest.id,
                    "tracker_id": latest.tracker_id,
                    "vehicle_type": latest.vehicle_type,
                    "registration_number": latest.registration_number,
                    "confidence": latest.confidence,
                    # The current Detection model has no plate_confidence column.
                    "ocr_confidence": None,
                    "severity_score": latest.severity_score,
                    "overload_status": latest.overload_status,
                    "location": latest.location,
                    "snapshot": format_snapshot(latest.snapshot),
                    "timestamp": (
                        latest.timestamp.isoformat()
                        if latest.timestamp is not None
                        else None
                    ),
                    "latitude": latest.latitude,
                    "longitude": latest.longitude,
                }
            ]
        }

    finally:
        db.close()


# ==========================================================
# GET DASHBOARD STATISTICS
# ==========================================================

def get_dashboard_stats():
    db: Session = SessionLocal()

    try:
        detections = db.query(Detection).all()

        vehicle_types = [
            (d.vehicle_type or "").strip().lower()
            for d in detections
        ]
        statuses = [
            (d.overload_status or "").strip().upper()
            for d in detections
        ]

        return {
            "total_vehicles": len(detections),
            "cars": vehicle_types.count("car"),
            "trucks": vehicle_types.count("truck"),
            "buses": vehicle_types.count("bus"),
            "motorcycles": vehicle_types.count("motorcycle"),
            "taxis": vehicle_types.count("taxi"),
            "vans": vehicle_types.count("van"),
            "pickups": vehicle_types.count("pickup"),
            "warnings": statuses.count("WARNING"),
            "overloaded": sum(
                1 for status in statuses
                if status in {"HIGH", "OVERLOADED"}
            ),
        }

    finally:
        db.close()


# ==========================================================
# GET ALERTS
# ==========================================================

def get_alerts():
    db: Session = SessionLocal()

    try:
        return (
            db.query(Detection)
            .filter(Detection.overload_status != "NORMAL")
            .order_by(desc(Detection.timestamp))
            .all()
        )
    finally:
        db.close()


# ==========================================================
# CLEAR ALERTS
# ==========================================================

def clear_alerts():
    db: Session = SessionLocal()

    try:
        deleted = (
            db.query(Detection)
            .filter(Detection.overload_status != "NORMAL")
            .delete(synchronize_session=False)
        )
        db.commit()

        return {
            "deleted_records": deleted,
            "message": "Alerts cleared successfully.",
        }

    except Exception as exc:
        db.rollback()
        print(f"[DATABASE] Clear alerts failed: {exc}")
        raise

    finally:
        db.close()


# ==========================================================
# GET DETECTIONS BY VEHICLE TYPE
# ==========================================================

def get_detections_by_vehicle(vehicle_type: str):
    db: Session = SessionLocal()

    try:
        return (
            db.query(Detection)
            .filter(Detection.vehicle_type == vehicle_type)
            .order_by(desc(Detection.timestamp))
            .all()
        )
    finally:
        db.close()


# ==========================================================
# GET DETECTION BY ID
# ==========================================================

def get_detection_by_id(detection_id: int):
    db: Session = SessionLocal()

    try:
        return (
            db.query(Detection)
            .filter(Detection.id == detection_id)
            .first()
        )
    finally:
        db.close()
