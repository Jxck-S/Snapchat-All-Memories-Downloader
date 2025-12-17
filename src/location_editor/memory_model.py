"""Editor-specific relaxed Memory model for the Location Editor GUI."""

import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ConfigDict

from ..memory import MediaType


class EditorMemory(BaseModel):
    """Minimal Memory model for the editor program (no OCR, no timezone).

    - Only the fields the editor needs
    - Core fields optional to tolerate partial records
    - Unknown/extra fields are ignored
    - Serialization uses Snapchat aliases and drops raw Location
    """

    # Ensure all datetimes serialize back to Snapchat JSON string format
    model_config = ConfigDict(
        extra="ignore",
        json_encoders={
            datetime: lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        },
    )

    date: Optional[datetime] = Field(default=None, validation_alias="Date")
    media_type: Optional[MediaType] = Field(default=None, validation_alias="Media Type")
    media_download_url: Optional[str] = Field(default=None, validation_alias="Media Download Url")
    download_link: Optional[str] = Field(default="", validation_alias="Download Link")
    latitude: Optional[float] = Field(default=None)
    longitude: Optional[float] = Field(default=None)
    manual_location: bool = Field(default=False)
    location_available: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_and_parse_location(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        # Dynamically copy values both ways between field names and their validation_alias
        for field_name, field_info in cls.model_fields.items():
            alias = getattr(field_info, "validation_alias", None)
            if alias:
                # alias -> field name, without removing alias
                if field_name not in normalized and alias in normalized:
                    normalized[field_name] = normalized[alias]
                # field name -> alias, so validation_alias can find it
                if alias not in normalized and field_name in normalized:
                    normalized[alias] = normalized[field_name]

        # Parse Location string into latitude/longitude if present
        location_str = normalized.pop("Location", None) or normalized.pop("location", None)
        if location_str and not normalized.get("latitude"):
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", location_str):
                normalized["latitude"] = float(match.group(1))
                normalized["longitude"] = float(match.group(2))

        return normalized

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, v):
        if isinstance(v, str) and v:
            dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S UTC")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return v

    @field_validator("media_type", mode="before")
    @classmethod
    def parse_media_type(cls, v):
        if isinstance(v, str) and v:
            return MediaType(v.lower())
        return v

    def model_post_init(self, __context):
        # Only set location_available flag
        if self.latitude is not None and self.longitude is not None:
            self.location_available = True
        else:
            self.location_available = False

    def to_serialized_dict(self) -> dict:
        """Dump for export with clean keys and exclusions."""
        data = self.model_dump(by_alias=True, mode="json")
        # Remove raw Location if present in source
        data.pop("Location", None)
        return data
