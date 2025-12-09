"""Data models for Snapchat memories."""

import re
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ConfigDict
from timezonefinder import TimezoneFinder
import pytz

from . import config
from .config import OverlayMode

# Module-level singleton for TimezoneFinder (initialized once for performance)
# TimezoneFinder loads timezone boundary data which is slow, so we reuse one instance
_timezone_finder_instance = TimezoneFinder()


class MediaType(str, Enum):
    """Enum for supported media types."""
    IMAGE = "image"
    VIDEO = "video"
    
    @classmethod
    def _missing_(cls, value):
        """Raise error for unsupported media types."""
        if value:
            raise ValueError(f"Unsupported media type: '{value}'. Must be 'image' or 'video'")
        return super()._missing_(value)


class Memory(BaseModel):
    # Ensure all datetimes serialize back to Snapchat JSON string format
    model_config = ConfigDict(json_encoders={
        datetime: lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    })
    """Model for a single memory from Snapchat export."""
    date: datetime = Field(serialization_alias="date")
    media_type: MediaType = Field(serialization_alias="media_type")
    media_download_url: str = Field(serialization_alias="media_download_url")  # Direct AWS CDN URL - has overlays (ZIP), rate limited
    download_link: str = Field(default="", serialization_alias="download_link")  # Snapchat endpoint - requires POST returns AWS URL, no overlays, no rate limit
    location: str = Field(default="", alias="Location")
    latitude: Optional[float] = Field(default=None)
    longitude: Optional[float] = Field(default=None)
    location_available: bool = Field(default=False, exclude=True)  # True if lat/lon are valid coordinates
    path_with_overlay: Optional[Path] = Field(default=None, exclude=True)
    path_without_overlay: Optional[Path] = Field(default=None, exclude=True)
    extracted_ocr_text: Optional[str] = Field(default=None, alias="extracted_ocr_text")
    manual_location: bool = Field(default=False)
    occurrence: int = Field(default=1, exclude=True)  # Which occurrence of this timestamp (1-based, for handling duplicates)

    # Per-field serializer is unnecessary because model_config.json_encoders
    # already formats all datetime values uniformly.

    @model_validator(mode="before")
    @classmethod
    def normalize_field_names(cls, data):
        """Support  (spaces) or (underscores)."""
        if not isinstance(data, dict):
            return data
        
        # Map old field names to new ones if they exist
        field_mappings = {
            "Date": "date",
            "Media Type": "media_type",
            "Media Download Url": "media_download_url",
            "Download Link": "download_link",
        }
        
        # Copy data to avoid modifying original
        normalized = dict(data)
        
        # If new format field doesn't exist, try old format name
        for old_name, new_name in field_mappings.items():
            if new_name not in normalized and old_name in normalized:
                normalized[new_name] = normalized.pop(old_name)
        
        return normalized

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str):
            # Parse from UTC (Snapchat JSON is always UTC)
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S UTC")
            dt = dt.replace(tzinfo=timezone.utc)
            # Keep as UTC - don't convert to local timezone
            return dt
        return v

    @field_validator("media_type", mode="before")
    @classmethod
    def parse_media_type(cls, v):
        if isinstance(v, str):
            # Convert to lowercase for enum matching (Image -> image, Video -> video)
            normalized = v.lower()
            return MediaType(normalized)
        return v

    def model_post_init(self, __context):
        if self.location and not self.latitude:
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", self.location):
                self.latitude = float(match.group(1))
                self.longitude = float(match.group(2))
        
        # Check if location data is valid (not 0.0, 0.0 null values)
        if self.latitude is not None and self.longitude is not None:
            # Valid coordinates available
            self.location_available = True
        else:
            self.location_available = False
        
        # Apply timezone awareness based on location
        self.apply_timezone_to_date()
    
    def model_dump(self, **kwargs):
        """Override model_dump to exclude Location field when latitude/longitude are present."""
        data = super().model_dump(**kwargs)
        
        # If we have latitude/longitude, don't export the Location string field
        if self.latitude is not None and self.longitude is not None:
            data.pop("Location", None)
        
        return data

    def get_filename(self, has_overlay: bool = False, occurrence: int = 1) -> str:
        """Get filename with optional '_overlayed' suffix for overlaid versions.
        
        Args:
            has_overlay: Whether this is an overlayed version
            occurrence: Which occurrence of this timestamp (1-based). 
                       For occurrence > 1, appends _v{occurrence} suffix to handle duplicates.
        """
        ext = ".jpg" if self.media_type == MediaType.IMAGE else ".mp4"
        base_name = self.date.strftime('%Y-%m-%d_%H-%M-%S')
        # Add version suffix for duplicates (timestamps with multiple entries)
        version_suffix = f"_v{occurrence}" if occurrence > 1 else ""
        overlay_suffix = "_overlayed" if has_overlay else ""
        prefix = f"{config.filename_prefix}_" if config.filename_prefix else ""
        return f"{prefix}{base_name}{version_suffix}{overlay_suffix}{ext}"

    def get_overlay_filename(self, occurrence: int = 1) -> str:
        """Get filename for the overlay file (WebP format).
        
        Args:
            occurrence: Which occurrence of this timestamp (1-based). 
                       For occurrence > 1, appends _v{occurrence} suffix to handle duplicates.
        """
        base_name = self.date.strftime('%Y-%m-%d_%H-%M-%S')
        version_suffix = f"_v{occurrence}" if occurrence > 1 else ""
        prefix = f"{config.filename_prefix}_" if config.filename_prefix else ""
        return f"{prefix}{base_name}{version_suffix}_overlay.webp"

    def get_media_download_url(self) -> str:
        """Get direct AWS CDN URL for media with overlays (ZIP format)."""
        return self.media_download_url

    async def get_cdn_url(self) -> str:
        """POST to Snapchat endpoint to get AWS CDN URL for media without overlays."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.download_link,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            return response.text.strip()

    def apply_timezone_to_date(self) -> None:
        """Apply timezone awareness to the date based on GPS location.
        
        Converts the UTC datetime to the local timezone of the memory location,
        accounting for DST on the capture date. This makes the datetime aware of
        the actual timezone where the memory was captured.
        
        Modifies self.date in-place to be timezone-aware in the local timezone.
        """
        # Skip if location data is not available
        if not self.location_available:
            return
        
        try:
            tz_name = _timezone_finder_instance.timezone_at(lat=self.latitude, lng=self.longitude)
            
            if not tz_name:
                return
            
            # Get timezone object
            tz = pytz.timezone(tz_name)
            
            # Convert UTC datetime to local timezone (with DST applied automatically)
            local_dt = self.date.astimezone(tz)
            
            # Update the date field to be in the local timezone
            self.date = local_dt
        
        except Exception as e:
            print(f"Failed to apply timezone for ({self.latitude}, {self.longitude}): {e}")

    def fix_paths_on_merge_failure(self, overlay_mode: OverlayMode) -> None:
        """Fix memory file paths when overlay merge fails.
        
        For 'both' mode:
        - Clear overlay path
        - Keep non-overlay path
        
        For 'with' mode:
        - Move overlay path to non-overlay path (rename to remove _overlayed suffix)
        - Clear overlay path
        """
        if overlay_mode == OverlayMode.BOTH:
            # Clear the overlay version path, keep the non-overlay
            if self.path_with_overlay:
                self.path_with_overlay.unlink(missing_ok=True)
            self.path_with_overlay = None
        elif overlay_mode == OverlayMode.WITH:
            # Move overlay path to non-overlay path with cleaned filename
            if self.path_with_overlay and self.path_with_overlay.exists():
                # Create non-overlay filename by removing "_overlayed" suffix
                new_filename = self.path_with_overlay.name.replace("_overlayed", "")
                new_path = self.path_with_overlay.parent / new_filename
                self.path_with_overlay.rename(new_path)
                self.path_without_overlay = new_path
                self.path_with_overlay = None
