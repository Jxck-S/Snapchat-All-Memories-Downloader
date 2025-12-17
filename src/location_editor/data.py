"""Data models for Location Editor."""

from typing import Optional


class LocationData:
    """Store location data for a memory."""
    
    def __init__(self, timestamp: str, latitude: float = None, longitude: float = None, skipped: bool = False):
        self.timestamp = timestamp
        self.latitude = latitude
        self.longitude = longitude
        self.skipped = skipped
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        if self.skipped:
            return {
                "timestamp": self.timestamp,
                "skipped": True
            }
        return {
            "timestamp": self.timestamp,
            "latitude": self.latitude,
            "longitude": self.longitude
        }
