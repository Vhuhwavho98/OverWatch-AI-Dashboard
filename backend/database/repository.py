# ==========================================================
# OverWatch AI - CSV Repository
# Stores and retrieves vehicle detections using detections.csv
# No SQL Server or database connection required.
# ==========================================================

import csv
import threading
from pathlib import Path
from datetime import datetime, timedelta

# repository.py is expected to be in backend/database/
# This creates backend/detections.csv.
CSV_FILE = Path(__file__).resolve().parents[1] / "detections.csv"

CSV_FIELDS = [
    "id",
    "tracker_id",
    "vehicle_type",
    "registration_number",
    "confidence",
    "overload_status",
    "severity_score",
    "location",
    "snapshot",
    "plate_snippet",
    "timestamp",
    "latitude",
    "longitude",
]

# Prevent simultaneous writes from threads in this Python process.
_CSV_LOCK = threading.RLock()

UNKNOWN_REGISTRATIONS = {
    "",
    "UNKNOWN",
    "OCR NOT READABLE",
    "NEEDS REVIEW",
    "N/A",
    "NONE",
}


class DetectionRecord(dict):
    """Dictionary record that also supports attribute access (record.id)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _clean_registration(value):
    """Return a normalised registration, or None for an unreadable result."""
    if value is None:
        return None

    cleaned = " ".join(str(value).strip().upper().split())

    if cleaned in UNKNOWN_REGISTRATIONS:
        return None

    return cleaned


def format_snapshot(snapshot):
    """Convert a stored snapshot path into a URL served by FastAPI."""
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


def format_plate_snippet(plate_snippet):
    """Convert a stored plate-crop path into a URL served by FastAPI."""
    if not plate_snippet:
        return None

    value = str(plate_snippet).replace("\\\\", "/")

    if value.startswith(("http://", "https://")):
        return value

    if value.startswith("/uploads/"):
        return value

    marker = "uploads/"
    if marker in value:
        return "/" + marker + value.split(marker, 1)[1].lstrip("/")

    return f"/uploads/plates/{Path(value).name}"


def _ensure_csv():
    """Create the CSV or safely add newly introduced columns to an old file."""
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
        with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()
        return

    # Existing detections.csv files may predate the plate_snippet column.
    # Migrate their header without discarding any saved detections.
    with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        old_fields = reader.fieldnames or []
        if all(field in old_fields for field in CSV_FIELDS):
            return
        existing_rows = list(reader)

    temporary_file = CSV_FILE.with_suffix(".csv.migrating")
    with temporary_file.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in existing_rows:
            writer.writerow({field: row.get(field, "") or "" for field in CSV_FIELDS})
    temporary_file.replace(CSV_FILE)


def _to_record(row):
    """Convert a CSV row into a consistently typed detection record."""
    record = dict(row)

    try:
        record["id"] = int(record.get("id") or 0)
    except (TypeError, ValueError):
        record["id"] = 0

    try:
        record["tracker_id"] = int(record.get("tracker_id") or 0)
    except (TypeError, ValueError):
        record["tracker_id"] = 0

    try:
        record["confidence"] = float(record.get("confidence") or 0.0)
    except (TypeError, ValueError):
        record["confidence"] = 0.0

    try:
        record["severity_score"] = int(float(record.get("severity_score") or 0))
    except (TypeError, ValueError):
        record["severity_score"] = 0

    for field in ("latitude", "longitude"):
        value = record.get(field)
        try:
            record[field] = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            record[field] = None

    return DetectionRecord(record)


def _read_rows():
    """Read all records from the CSV file."""
    with _CSV_LOCK:
        _ensure_csv()
        with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as file:
            return [_to_record(row) for row in csv.DictReader(file)]


def _write_rows(rows):
    """Atomically replace the CSV contents with the supplied records."""
    with _CSV_LOCK:
        _ensure_csv()
        temporary_file = CSV_FILE.with_suffix(".csv.tmp")

        with temporary_file.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()

            for row in rows:
                writer.writerow({
                    field: row.get(field, "")
                    if row.get(field) is not None
                    else ""
                    for field in CSV_FIELDS
                })

        temporary_file.replace(CSV_FILE)


def _next_id(rows):
    return max((int(row.get("id") or 0) for row in rows), default=0) + 1


def _timestamp_value(value):
    """Parse a stored ISO timestamp, returning None when unavailable."""
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def _apply_optional_fields(
    detection,
    *,
    vehicle_type=None,
    confidence=None,
    overload_status=None,
    severity_score=None,
    location=None,
    snapshot=None,
    plate_snippet=None,
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
        "plate_snippet": plate_snippet,
        "latitude": latitude,
        "longitude": longitude,
    }

    for field, value in values.items():
        if value is not None:
            detection[field] = value


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
    plate_snippet: str = None,
    latitude: float = None,
    longitude: float = None,
):
    """Insert one vehicle detection into detections.csv."""
    try:
        with _CSV_LOCK:
            rows = _read_rows()
            detection = DetectionRecord({
                "id": _next_id(rows),
                "tracker_id": int(tracker_id),
                "vehicle_type": vehicle_type,
                "registration_number": _clean_registration(registration_number) or "UNKNOWN",
                "confidence": float(confidence or 0.0),
                "overload_status": overload_status or "INDETERMINATE",
                "severity_score": int(severity_score or 0),
                "location": location or "Unknown",
                "snapshot": snapshot or "",
                "plate_snippet": plate_snippet or "",
                "timestamp": datetime.utcnow().isoformat(),
                "latitude": latitude,
                "longitude": longitude,
            })

            rows.append(detection)
            _write_rows(rows)
            return detection

    except Exception as exc:
        print(f"[CSV] Save detection failed: {exc}")
        raise


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
    plate_snippet: str = None,
    latitude: float = None,
    longitude: float = None,
    max_age_minutes: int = 5,
):
    """
    Update the newest recent record for this tracker ID and location.

    A recognised ANPR result updates the registration. UNKNOWN OCR results
    never overwrite a registration that has already been recognised.
    """
    try:
        with _CSV_LOCK:
            rows = _read_rows()
            cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)

            matches = []
            for index, row in enumerate(rows):
                if int(row.get("tracker_id") or 0) != int(tracker_id):
                    continue

                if location is not None and row.get("location") != location:
                    continue

                recorded_at = _timestamp_value(row.get("timestamp"))
                if recorded_at is None or recorded_at < cutoff:
                    continue

                matches.append((index, recorded_at))

            if not matches:
                return None

            latest_index = max(matches, key=lambda item: item[1])[0]
            detection = rows[latest_index]

            registration = _clean_registration(registration_number)
            if registration:
                detection["registration_number"] = registration

            _apply_optional_fields(
                detection,
                vehicle_type=vehicle_type,
                confidence=confidence,
                overload_status=overload_status,
                severity_score=severity_score,
                location=location,
                snapshot=snapshot,
                plate_snippet=plate_snippet,
                latitude=latitude,
                longitude=longitude,
            )

            # Refresh timestamp to keep this track's record current.
            detection["timestamp"] = datetime.utcnow().isoformat()

            _write_rows(rows)
            return detection

    except Exception as exc:
        print(f"[CSV] Update detection failed: {exc}")
        raise


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
    plate_snippet: str = None,
    latitude: float = None,
    longitude: float = None,
):
    """
    Create a record on first sighting, then update it for the same recent track.
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
        plate_snippet=plate_snippet,
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
        plate_snippet=plate_snippet,
        latitude=latitude,
        longitude=longitude,
    )


# ==========================================================
# GET ALL DETECTIONS
# ==========================================================

def get_all_detections():
    """Return all detections, newest first."""
    rows = _read_rows()
    for row in rows:
        row["snapshot"] = format_snapshot(row.get("snapshot"))
        row["plate_snippet"] = format_plate_snippet(row.get("plate_snippet"))

    return sorted(
        rows,
        key=lambda row: _timestamp_value(row.get("timestamp")) or datetime.min,
        reverse=True,
    )


# ==========================================================
# GET LATEST DETECTION
# ==========================================================

def get_latest_detection():
    """Return the latest detection in the dashboard response format."""
    detections = get_all_detections()

    if not detections:
        return {"vehicles": []}

    latest = detections[0]

    return {
        "vehicles": [
            {
                "id": latest.id,
                "tracker_id": latest.tracker_id,
                "vehicle_type": latest.vehicle_type,
                "registration_number": latest.registration_number,
                "confidence": latest.confidence,
                "ocr_confidence": None,
                "severity_score": latest.severity_score,
                "overload_status": latest.overload_status,
                "location": latest.location,
                "snapshot": format_snapshot(latest.snapshot),
                "plate_snippet": format_plate_snippet(latest.get("plate_snippet")),
                "timestamp": latest.timestamp or None,
                "latitude": latest.latitude,
                "longitude": latest.longitude,
            }
        ]
    }


# ==========================================================
# GET DASHBOARD STATISTICS
# ==========================================================

def get_dashboard_stats():
    """Calculate dashboard totals from detections.csv."""
    detections = get_all_detections()

    vehicle_types = [
        (row.get("vehicle_type") or "").strip().lower()
        for row in detections
    ]
    statuses = [
        (row.get("overload_status") or "").strip().upper()
        for row in detections
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


# ==========================================================
# GET ALERTS
# ==========================================================

def get_alerts():
    """Return all records whose status is not NORMAL."""
    return [
        row for row in get_all_detections()
        if (row.get("overload_status") or "").strip().upper() != "NORMAL"
    ]


# ==========================================================
# CLEAR ALERTS
# ==========================================================

def clear_alerts():
    """Remove non-NORMAL detection records from detections.csv."""
    try:
        with _CSV_LOCK:
            rows = _read_rows()
            remaining = [
                row for row in rows
                if (row.get("overload_status") or "").strip().upper() == "NORMAL"
            ]
            deleted = len(rows) - len(remaining)
            _write_rows(remaining)

        return {
            "deleted_records": deleted,
            "message": "Alerts cleared successfully.",
        }

    except Exception as exc:
        print(f"[CSV] Clear alerts failed: {exc}")
        raise


# ==========================================================
# GET DETECTIONS BY VEHICLE TYPE
# ==========================================================

def get_detections_by_vehicle(vehicle_type: str):
    """Return detections matching a vehicle type."""
    requested_type = (vehicle_type or "").strip().lower()

    return [
        row for row in get_all_detections()
        if (row.get("vehicle_type") or "").strip().lower() == requested_type
    ]


# ==========================================================
# GET DETECTION BY ID
# ==========================================================

def get_detection_by_id(detection_id: int):
    """Return one detection by its CSV ID, or None."""
    for row in _read_rows():
        if row.id == int(detection_id):
            return row

    return None
