"""Data models for Snapchat memories."""

import re
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from . import config
from .config import OverlayMode, OverlayNaming


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
    """Model for a single memory from Snapchat export."""
    date: datetime = Field(alias="Date")
    media_type: MediaType = Field(alias="Media Type")
    media_download_url: str = Field(alias="Media Download Url")  # Direct AWS CDN URL - has overlays (ZIP), rate limited
    download_link: str = Field(default="", alias="Download Link")  # Snapchat endpoint - requires POST returns AWS URL, no overlays, no rate limit
    location: str = Field(default="", alias="Location")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    path_with_overlay: Optional[Path] = None
    path_without_overlay: Optional[Path] = None

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

    def get_filename(self, has_overlay: bool = False) -> str:
        """Get filename with optional '_overlayed' suffix for overlaid versions."""
        ext = ".jpg" if self.media_type == MediaType.IMAGE else ".mp4"
        base_name = self.date.strftime('%Y-%m-%d_%H-%M-%S')
        overlay_suffix = "_overlayed" if has_overlay else ""
        prefix = f"{config.filename_prefix}_" if config.filename_prefix else ""
        return f"{prefix}{base_name}{overlay_suffix}{ext}"

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
