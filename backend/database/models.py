# ==========================================================
# OverWatch AI - SQLAlchemy Models
# Matches SQL Server (OverloadDB)
# ==========================================================

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    DateTime,
    func,
)

from database.database import Base


# ==========================================================
# DETECTIONS TABLE
# SQL Server: dbo.detections
# ==========================================================

class Detection(Base):
    __tablename__ = "detections"

    # Primary Key
    id = Column(Integer, primary_key=True, index=True)

    # YOLO Tracking
    tracker_id = Column(Integer, nullable=False)

    # Vehicle Information
    vehicle_type = Column(String(50), nullable=False)
    registration_number = Column(String(30), default="UNKNOWN")

    # Detection Confidence
    confidence = Column(Float, nullable=False)

    # Overload Classification
    overload_status = Column(String(20), default="NORMAL")
    severity_score = Column(Integer, default=0)

    # GPS Location
    location = Column(String(255), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Saved Snapshot
    snapshot = Column(String(500), nullable=True)

    # Detection Timestamp
    timestamp = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tracker_id": self.tracker_id,
            "vehicle_type": self.vehicle_type,
            "registration_number": self.registration_number,
            "confidence": self.confidence,
            "overload_status": self.overload_status,
            "severity_score": self.severity_score,
            "location": self.location,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "snapshot": self.snapshot,
            
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }