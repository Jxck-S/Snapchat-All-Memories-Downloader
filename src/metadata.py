"""Metadata and timestamp handling for media files."""

import os
import subprocess
import piexif
from pathlib import Path
from datetime import timezone

from . import config
from .memory import Memory, MediaType


def _to_deg(value):
    """Convert decimal degrees to (deg, min, sec)."""
    d = int(abs(value))
    m_float = (abs(value) - d) * 60
    m = int(m_float)
    s = round((m_float - m) * 60, 6)
    return d, m, s


def _deg_to_rational(dms):
    """Convert (deg, min, sec) tuple to EXIF rational format."""
    d, m, s = dms
    return [
        (int(d), 1),
        (int(m), 1),
        (int(s * 100), 100)
    ]


def add_exif_data(image_path: Path, memory: Memory):
    """Add EXIF metadata to an image file.
    
    Embeds the following EXIF data into the image:
    - DateTime fields: Capture timestamp in UTC for accurate chronological sorting
    - Software tag: Identifies "Snapchat" as the source application
    - GPS data: Latitude, longitude, and direction references from memory.location
    
    Args:
        image_path: Path to the image file to modify
        memory: Memory object containing date, latitude, and longitude data
    
    Updates the image file in-place with embedded EXIF data and sets filesystem
    timestamp to match the memory's capture date.
    
    Gracefully handles missing EXIF data by creating new EXIF structure if needed.
    Errors during EXIF embedding are logged but don't stop the download process.
    """
    try:
        # Load existing EXIF if any
        try:
            exif_dict = piexif.load(str(image_path))
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        # Date/time in UTC (EXIF standard for accurate timestamps)
        dt_utc = memory.date.astimezone(timezone.utc) if memory.date.tzinfo else memory.date.replace(tzinfo=timezone.utc)
        dt_str = dt_utc.strftime("%Y:%m:%d %H:%M:%S")
        # DateTime: File modification time (general timestamp field)
        exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_str
        # DateTimeOriginal: When photo was taken (original capture time)
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        # DateTimeDigitized: When photo was digitized (same as original for digital photos)
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str
        
        # Add application source (Snapchat) - must be bytes
        # Software: Identifies the app that created/processed the image
        exif_dict["0th"][piexif.ImageIFD.Software] = b"Snapchat"

        # GPS if available
        if memory.latitude is not None and memory.longitude is not None:
            lat_ref = "N" if memory.latitude >= 0 else "S"
            lon_ref = "E" if memory.longitude >= 0 else "W"
            lat_dms = _deg_to_rational(_to_deg(memory.latitude))
            lon_dms = _deg_to_rational(_to_deg(memory.longitude))

            # GPSLatitudeRef: Direction (N=North, S=South)
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = lat_ref.encode()
            # GPSLongitudeRef: Direction (E=East, W=West)
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = lon_ref.encode()
            # GPSLatitude: Latitude as (degrees, minutes, seconds)
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = lat_dms
            # GPSLongitude: Longitude as (degrees, minutes, seconds)
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = lon_dms
            # GPSVersionID: GPS IFD version (2.3.0.0)
            exif_dict["GPS"][piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)

        # Dump and insert
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(image_path))

        # Update filesystem timestamp
        ts = memory.date.timestamp()
        os.utime(image_path, (ts, ts))

    except Exception as e:
        print(f"Failed to set EXIF data for {image_path.name}: {e}")


def set_video_metadata(video_path: Path, memory: Memory):
    """
    Sets video creation time and Apple Photos-compatible GPS metadata.
    Uses ffmpeg via subprocess to inject metadata without re-encoding.
    """
    try:
        # Prepare UTC creation time in ISO 8601
        dt_utc = memory.date.astimezone(timezone.utc)
        iso_time = dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        # Base metadata arguments with application source using ©too tag (QuickTime software tag)
        metadata_args = [
            "-metadata", f"creation_time={iso_time}",
            "-metadata", "©too=Snapchat",  # QuickTime software tag - standardized for app identification
        ]

        # Add location if available
        if memory.latitude is not None and memory.longitude is not None:
            lat = f"{memory.latitude:+.4f}"
            lon = f"{memory.longitude:+.4f}"
            alt = getattr(memory, "altitude", 0.0)
            iso6709 = f"{lat}{lon}+{alt:.3f}/"

            # Apple Photos-compatible fields
            metadata_args += [
                "-metadata", f"location={iso6709}",
                "-metadata", f"location-eng={iso6709}",
            ]

        # Temporary output file
        temp_path = video_path.with_suffix(".temp.mp4")

        # Run ffmpeg: copy streams, inject metadata
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i", str(video_path),
                *metadata_args,
                "-codec", "copy",
                "-movflags", "use_metadata_tags",
                str(temp_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Replace original file
        temp_path.replace(video_path)

        # Update filesystem timestamp
        os.utime(video_path, (memory.date.timestamp(), memory.date.timestamp()))

    except Exception as e:
        print(f"Failed to set video metadata for {video_path.name}: {e}")


def _apply_metadata_to_path(file_path: Path, memory: Memory, timestamp: float) -> None:
    """Helper function to apply metadata to a single file path."""
    if not file_path.exists():
        return
    
    os.utime(file_path, (timestamp, timestamp))
    
    if not config.add_exif:
        return
    
    if memory.media_type == MediaType.IMAGE:
        add_exif_data(file_path, memory)
    elif memory.media_type == MediaType.VIDEO and config.ffmpeg_available:
        set_video_metadata(file_path, memory)


def apply_metadata_and_timestamps(memory: Memory, add_exif: bool) -> None:
    """Apply metadata and timestamps to downloaded media files."""
    timestamp = memory.date.timestamp()
    
    # Apply to path_with_overlay if set
    if memory.path_with_overlay is not None:
        _apply_metadata_to_path(memory.path_with_overlay, memory, timestamp)
    
    # Apply to path_without_overlay if set
    if memory.path_without_overlay is not None:
        _apply_metadata_to_path(memory.path_without_overlay, memory, timestamp)
