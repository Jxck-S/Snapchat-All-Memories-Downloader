import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
import io
import zipfile
from PIL import Image
import tempfile
from zoneinfo import ZoneInfo
import piexif
import httpx
from pydantic import BaseModel, Field, field_validator
from tqdm.asyncio import tqdm
from tzlocal import get_localzone

# Global configuration
ffmpeg_path: str = "ffmpeg"
overlay_mode: str = "none"
overlay_naming: str = "separate-folders"
output_dir: Path = Path("./downloads")
max_concurrent: int = 40
add_exif: bool = True
skip_existing: bool = True
filename_prefix: str = ""


class Memory(BaseModel):
    date: datetime = Field(alias="Date")
    media_type: str = Field(alias="Media Type")
    media_download_url: str = Field(alias="Media Download Url")  # Direct AWS CDN URL - has overlays (ZIP), rate limited
    download_link: str = Field(default="", alias="Download Link")  # Snapchat endpoint - requires POST returns AWS URL, no overlays, no rate limit
    location: str = Field(default="", alias="Location")
    latitude: float | None = None
    longitude: float | None = None
    path_with_overlay: Path | None = None
    path_without_overlay: Path | None = None

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


    def model_post_init(self, __context):
        if self.location and not self.latitude:
            if match := re.search(r"([-\d.]+),\s*([-\d.]+)", self.location):
                self.latitude = float(match.group(1))
                self.longitude = float(match.group(2))

    def get_filename(self, has_overlay: bool = False) -> str:
        """Get filename with optional '_overlayed' suffix for overlaid versions."""
        ext = ".jpg" if self.media_type.lower() == "image" else ".mp4"
        base_name = self.date.strftime('%Y-%m-%d_%H-%M-%S')
        overlay_suffix = "_overlayed" if has_overlay else ""
        prefix = f"{filename_prefix}_" if filename_prefix else ""
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

    def fix_paths_on_merge_failure(self, overlay_mode: str) -> None:
        """Fix memory file paths when overlay merge fails.
        
        For 'both' mode:
        - Clear overlay path
        - Keep non-overlay path
        
        For 'with' mode:
        - Move overlay path to non-overlay path (rename to remove _overlayed suffix)
        - Clear overlay path
        """
        if overlay_mode == "both":
            # Clear the overlay version path, keep the non-overlay
            if self.path_with_overlay:
                self.path_with_overlay.unlink(missing_ok=True)
            self.path_with_overlay = None
        elif overlay_mode == "with":
            # Move overlay path to non-overlay path with cleaned filename
            if self.path_with_overlay and self.path_with_overlay.exists():
                # Create non-overlay filename by removing "_overlayed" suffix
                new_filename = self.path_with_overlay.name.replace("_overlayed", "")
                new_path = self.path_with_overlay.parent / new_filename
                self.path_with_overlay.rename(new_path)
                self.path_without_overlay = new_path
                self.path_with_overlay = None


class Stats(BaseModel):
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    overlay_failed: int = 0
    mb: float = 0
    # Media type counters
    total_images: int = 0
    total_videos: int = 0
    images_with_overlay: int = 0
    images_without_overlay: int = 0
    videos_with_overlay: int = 0
    videos_without_overlay: int = 0
    # Both mode extra copies (separate files without overlay)
    extra_images_without_overlay: int = 0
    extra_videos_without_overlay: int = 0


def load_memories(json_path: Path) -> list[Memory]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Memory(**item) for item in data["Saved Media"]]

async def get_cdn_url(download_link: str) -> str:
    """POST to Snapchat endpoint to get AWS CDN URL for media without overlays."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            download_link,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.text.strip()

def add_exif_data(image_path: Path, memory: Memory):
    def to_deg(value):
        """Convert decimal degrees to (deg, min, sec)."""
        d = int(abs(value))
        m_float = (abs(value) - d) * 60
        m = int(m_float)
        s = round((m_float - m) * 60, 6)
        return d, m, s

    def deg_to_rational(dms):
        """Convert (deg, min, sec) tuple to EXIF rational format."""
        d, m, s = dms
        return [
            (int(d), 1),
            (int(m), 1),
            (int(s * 100), 100)
        ]

    try:
        # Load existing EXIF if any
        try:
            exif_dict = piexif.load(str(image_path))
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        # Date/time in UTC (EXIF standard for accurate timestamps)
        dt_utc = memory.date.astimezone(timezone.utc) if memory.date.tzinfo else memory.date.replace(tzinfo=timezone.utc)
        dt_str = dt_utc.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str
        
        # Add application source (Snapchat) - must be bytes
        exif_dict["0th"][piexif.ImageIFD.Software] = b"Snapchat"

        # GPS if available
        if memory.latitude is not None and memory.longitude is not None:
            lat_ref = "N" if memory.latitude >= 0 else "S"
            lon_ref = "E" if memory.longitude >= 0 else "W"
            lat_dms = deg_to_rational(to_deg(memory.latitude))
            lon_dms = deg_to_rational(to_deg(memory.longitude))

            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = lat_ref.encode()
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = lon_ref.encode()
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = lat_dms
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = lon_dms
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

def apply_metadata_and_timestamps(memory: Memory, add_exif: bool) -> None:
    """Apply metadata and timestamps to downloaded media files."""
    timestamp = memory.date.timestamp()
    
    # Apply to path_with_overlay if set
    if memory.path_with_overlay is not None:
        if memory.path_with_overlay.exists():
            os.utime(memory.path_with_overlay, (timestamp, timestamp))
            if add_exif:
                if memory.media_type.lower() == "image":
                    add_exif_data(memory.path_with_overlay, memory)
                elif memory.media_type.lower() == "video":
                    set_video_metadata(memory.path_with_overlay, memory)
    
    # Apply to path_without_overlay if set
    if memory.path_without_overlay is not None:
        if memory.path_without_overlay.exists():
            os.utime(memory.path_without_overlay, (timestamp, timestamp))
            if add_exif:
                if memory.media_type.lower() == "image":
                    add_exif_data(memory.path_without_overlay, memory)
                elif memory.media_type.lower() == "video":
                    set_video_metadata(memory.path_without_overlay, memory)



def convert_webp_to_png(overlay_data: bytes) -> bytes:
    """Check if overlay is WebP format and convert to PNG in memory if needed."""
    try:
        # Check if it's WebP format by looking at RIFF signature
        if overlay_data.startswith(b'RIFF') and b'WEBP' in overlay_data[:12]:
            # It's WebP, convert to PNG
            img = Image.open(io.BytesIO(overlay_data))
            output = io.BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
        else:
            # Not WebP, return as-is
            return overlay_data
    except Exception as e:
        print(f"Failed to convert WebP to PNG: {e}")
        return overlay_data

def merge_image_overlay(output_path: Path, main_data: bytes, overlay_data: bytes | None, memory: Memory | None = None) -> None:
    """Merge image with optional overlay using PIL."""
    try:
        with Image.open(io.BytesIO(main_data)).convert("RGBA") as main_img:
            if overlay_data:
                try:
                    with Image.open(io.BytesIO(overlay_data)).convert("RGBA") as overlay_img:
                        overlay_resized = overlay_img.resize(main_img.size, Image.LANCZOS)
                        main_img.alpha_composite(overlay_resized)
                except Exception as e:
                    if memory:
                        print(f"Failed to load overlay for {memory.get_filename()}: {e}")
                    else:
                        print(f"Failed to load overlay image: {e}")
                    memory.fix_paths_on_merge_failure(overlay_mode)
                    memory.path_without_overlay.write_bytes(main_data)
                    print(f"Saved version without overlay: {memory.path_without_overlay}")
                    raise
            merged_img = main_img.convert("RGB")
            merged_img.save(output_path, "JPEG", quality=95, optimize=False)
    except Exception as e:
        if memory:
            print(f"Failed to process image {memory.get_filename()}: {e}")
        else:
            print(f"Failed to process image: {e}")
        raise

async def merge_video_overlay(
    output_path: Path, main_data: bytes, overlay_data: bytes | None, memory: Memory
) -> None:
    """Merge video with optional overlay using ffmpeg."""
    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = Path(tmpdir) / "main.mp4"
        merged_path = Path(tmpdir) / "merged.mp4"
        main_path.write_bytes(main_data)

        if overlay_data:
            overlay_path = Path(tmpdir) / "overlay.png"
            overlay_path.write_bytes(overlay_data)
            try:
                process = await asyncio.create_subprocess_exec(
                    ffmpeg_path,
                    "-y",
                    "-i",
                    str(main_path),
                    "-i",
                    str(overlay_path),
                    "-filter_complex",
                    "[1][0]scale2ref=w=iw:h=ih[overlay][base];[base][overlay]overlay=(W-w)/2:(H-h)/2",
                    "-codec:a",
                    "copy",
                    str(merged_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                if process.returncode == 0:
                    output_path.write_bytes(merged_path.read_bytes())
                else:
                    error_msg = stderr.decode() if stderr else "Unknown error"
                    print(f"ffmpeg overlay merge failed for {memory.get_filename(True)}: {error_msg}")
                    memory.fix_paths_on_merge_failure(overlay_mode)
                    memory.path_without_overlay.write_bytes(main_data)
                    print(f"Saved version without overlay: {memory.path_without_overlay}")
                    raise RuntimeError(f"ffmpeg merge failed: {error_msg}")
            except FileNotFoundError as e:
                print(f"Not found for {memory.get_filename(True)}.")
                raise
        else:
            output_path.write_bytes(main_data)


async def process_zip_with_overlays(output_path: Path, zip_content: bytes, memory: Memory, stats: Stats) -> None:
    """Extract and merge media from ZIP file with overlays.
    
    Handles different overlay modes:
    - 'with': Save only merged version with overlays to output_path
    - 'both': Save merged version to 'with_overlays' subfolder and main-only to 'without_overlays' subfolder
    
    If merge fails, saves the unextracted ZIP to an error folder for manual inspection.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
            files = zf.namelist()
            main_file = next((f for f in files if "-main" in f), None)
            overlay_file = next((f for f in files if "-overlay" in f), None)

            if not main_file:
                raise ValueError("No main media file found in ZIP.")

            main_data = zf.read(main_file)
            overlay_data = zf.read(overlay_file) if overlay_file else None
            
            # Convert WebP overlays to PNG format in memory
            if overlay_data:
                overlay_data = convert_webp_to_png(overlay_data)

            if overlay_mode == "both":
                if overlay_naming == "single-folder":
                    overlay_memory_path = output_dir / memory.get_filename(has_overlay=True)
                    no_overlay_memory_path = output_dir / memory.get_filename(has_overlay=False)
                elif overlay_naming == "separate-folders":
                    overlay_dir = output_dir / "with_overlays"
                    no_overlay_dir = output_dir / "without_overlays"
                    overlay_memory_path = overlay_dir / memory.get_filename(has_overlay=True)
                    no_overlay_memory_path = no_overlay_dir / memory.get_filename(has_overlay=False)
                # Save version with overlays
                memory.path_with_overlay = overlay_memory_path
                memory.path_without_overlay = no_overlay_memory_path
                if memory.media_type.lower() == "image":
                    merge_image_overlay(overlay_memory_path, main_data, overlay_data, memory)
                    stats.total_images += 1
                    stats.images_with_overlay += 1
                elif memory.media_type.lower() == "video":
                    await merge_video_overlay(overlay_memory_path, main_data, overlay_data, memory)
                    stats.total_videos += 1
                    stats.videos_with_overlay += 1
                else:
                    raise ValueError(f"Unsupported media type: {memory.media_type}")

                # Save version without overlays (main only - no merge needed)
                no_overlay_memory_path.write_bytes(main_data)
                # Count the extra copy
                if memory.media_type.lower() == "image":
                    stats.extra_images_without_overlay += 1
                else:
                    stats.extra_videos_without_overlay += 1
            else:
                # 'with' mode: save only merged version with overlays to output_path
                memory_path = output_path / memory.get_filename(has_overlay=True)
                memory.path_with_overlay = memory_path
                if memory.media_type.lower() == "image":
                    merge_image_overlay(memory_path, main_data, overlay_data, memory)
                    stats.total_images += 1
                    stats.images_with_overlay += 1
                elif memory.media_type.lower() == "video":
                    await merge_video_overlay(memory_path, main_data, overlay_data, memory)
                    stats.total_videos += 1
                    stats.videos_with_overlay += 1
                else:
                    raise ValueError(f"Unsupported media type: {memory.media_type}")
    except Exception as e:
        stats.overlay_failed += 1
        print(f"Error processing ZIP for {memory.get_filename()}: {e}")
        print(f"Saving extracted files to error folder for manual inspection.")
        
        # Extract and save files to error subfolder
        error_dir = output_dir / "error_zips" / memory.get_filename().rsplit('.', 1)[0]
        error_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                for file_info in zf.filelist:
                    file_data = zf.read(file_info.filename)
                    error_file_path = error_dir / file_info.filename
                    error_file_path.parent.mkdir(parents=True, exist_ok=True)
                    error_file_path.write_bytes(file_data)
                    print(f"  Saved: {error_file_path.relative_to(output_dir)}")
        except Exception as extract_error:
            print(f"Could not extract ZIP contents, saving raw ZIP file instead: {extract_error}")
            error_zip_path = error_dir.parent / f"{memory.get_filename().rsplit('.', 1)[0]}.zip"
            error_zip_path.write_bytes(zip_content)
            print(f"  Saved ZIP ({len(zip_content)} bytes) to: {error_zip_path.relative_to(output_dir)}")


async def download_memory(
    memory: Memory, add_exif: bool, semaphore: asyncio.Semaphore, stats: Stats
) -> tuple[bool, int]:
    async with semaphore:
        try:
            # Determine which URL to use based on overlay mode
            if overlay_mode in ("with", "both"):
                # Use media download URL (direct CDN with overlays)
                url = memory.get_media_download_url()
            else:
                # Use CDN endpoint (requires POST to get actual AWS URL)
                url = await memory.get_cdn_url()


            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.content

                # Direct download if no overlays or not a ZIP
                if overlay_mode == "none" or not response.headers.get("Content-Type", "").lower().startswith("application/zip"):
                    # Calculate filename based on path
                    if overlay_mode == "both" and overlay_naming == "separate-folders":
                        output_path = output_dir / "without_overlays" / memory.get_filename()
                    else:
                        output_path = output_dir / memory.get_filename()        
                    output_path.write_bytes(content)
                    memory.path_without_overlay = output_path
                    
                    # Update counters
                    if memory.media_type.lower() == "image":
                        stats.total_images += 1
                        stats.images_without_overlay += 1
                    else:
                        stats.total_videos += 1
                        stats.videos_without_overlay += 1
                else:
                    # Process ZIP with overlays
                    await process_zip_with_overlays(output_dir, content, memory, stats)

                bytes_downloaded = len(content)
                # Apply metadata and timestamps
                apply_metadata_and_timestamps(memory, add_exif)

                # Always return success + byte count
                return True, bytes_downloaded

        except Exception as e:
            print(f"\nError downloading {memory.get_filename()}: {e}")
            return False, 0



async def download_all(
    memories: list[Memory],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Create overlay folders if using 'both' mode with 'separate-folders' naming
    if overlay_mode == "both" and overlay_naming == "separate-folders":
        with_overlays_dir = output_dir / "with_overlays"
        without_overlays_dir = output_dir / "without_overlays"
        with_overlays_dir.mkdir(parents=True, exist_ok=True)
        without_overlays_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(max_concurrent)
    stats = Stats()
    start_time = time.time()

    def file_exists_in_tree(output_dir: Path, filename: str) -> bool:
        """Check if a file with the base name exists anywhere in output_dir tree."""
        # Get base name without extension and overlay suffix
        base_name = filename.rsplit(".", 1)[0].replace("_overlayed", "")
        # Search for any file that starts with the base name
        for file_path in output_dir.rglob("*"):
            if file_path.is_file():
                file_base = file_path.stem.replace("_overlayed", "")
                if file_base == base_name:
                    return True
        return False

    to_download = []
    for memory in memories:
        if skip_existing and file_exists_in_tree(output_dir, memory.get_filename()):
            stats.skipped += 1
        else:
            to_download.append(memory)

    if not to_download:
        print("All files already downloaded!")
        return

    progress_bar = tqdm(
        total=len(to_download),
        desc="Downloading",
        unit="file",
        disable=False,
    )

    async def process_and_update(memory):
        success, bytes_downloaded = await download_memory(
            memory, add_exif, semaphore, stats
        )
        if success:
            stats.downloaded += 1
        else:
            stats.failed += 1
        stats.mb += bytes_downloaded / 1024 / 1024

        elapsed = time.time() - start_time
        mb_per_sec = (stats.mb) / elapsed if elapsed > 0 else 0
        progress_bar.set_postfix({"MB/s": f"{mb_per_sec:.2f}"}, refresh=False)
        progress_bar.update(1)

    await asyncio.gather(*[process_and_update(m) for m in to_download])

    progress_bar.close()
    elapsed = time.time() - start_time
    mb_total = stats.mb
    mb_per_sec = mb_total / elapsed if elapsed > 0 else 0
    
    # Print comprehensive statistics
    print(f"\n{'='*70}")
    print(f"DOWNLOAD SUMMARY")
    print(f"{'='*70}")
    print(f"Downloaded: {stats.downloaded} | Skipped: {stats.skipped} | Failed: {stats.failed}")
    print(f"Total Size: {mb_total:.1f} MB @ {mb_per_sec:.2f} MB/s")
    print(f"{'='*70}")
    print(f"MEDIA BREAKDOWN")
    print(f"{'='*70}")
    print(f"Images:  {stats.total_images:4d} total | {stats.images_with_overlay:4d} with overlay | {stats.images_without_overlay:4d} without overlay")
    print(f"Videos:  {stats.total_videos:4d} total | {stats.videos_with_overlay:4d} with overlay | {stats.videos_without_overlay:4d} without overlay")
    if overlay_mode == "both" and (stats.extra_images_without_overlay > 0 or stats.extra_videos_without_overlay > 0):
        print(f"{'='*70}")
        print(f"BOTH MODE EXTRA COPIES")
        print(f"{'='*70}")
        print(f"Extra images (without overlay): {stats.extra_images_without_overlay}")
        print(f"Extra videos (without overlay): {stats.extra_videos_without_overlay}")
    if stats.overlay_failed > 0:
        print(f"{'='*70}")
        print(f"OVERLAY FAILURES")
        print(f"{'='*70}")
        print(f"Overlay merge failed: {stats.overlay_failed}")
    print(f"{'='*70}")


async def main():
    parser = argparse.ArgumentParser(
        description="Download Snapchat memories from data export (new JSON format)"
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="json/memories_history.json",
        help="Path to memories_history.json",
    )
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory"
    )
    parser.add_argument(
        "-c", "--concurrent", type=int, default=40, help="Max concurrent downloads"
    )
    parser.add_argument("--no-exif", action="store_true", help="Disable metadata writing")
    parser.add_argument(
        "--no-skip-existing", action="store_true", help="Re-download existing files"
    )
    parser.add_argument(
        "--overlay",
        choices=["none", "with", "both"],
        default="none",
        help="Overlay handling: 'none' = no overlays (fast), 'with' = with overlays only, 'both' = download with and without overlays into separate folders"
    )
    parser.add_argument(
        "--overlay-naming",
        choices=["separate-folders", "single-folder"],
        default="separate-folders",
        help="When --overlay is 'both': 'separate-folders' = split into with/without folders, 'single-folder' = all in one folder with '_overlayed' suffix for overlaid versions"
    )
    parser.add_argument(
        "--ffmpeg-path", default="ffmpeg", help="Path to ffmpeg executable (default: ffmpeg). Used for video metadata injection and overlay merging"
    )
    parser.add_argument(
        "--prefix", default="", help="Prefix to add to all downloaded filenames (e.g., 'SC_' creates 'SC_filename.ext')"
    )
    args = parser.parse_args()

    # Check if ffmpeg is needed and available
    if args.overlay in ("with", "both"):
        try:
            subprocess.run(
                [args.ffmpeg_path, "-version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"ffmpeg found at: {args.ffmpeg_path}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(f"Error: ffmpeg not found at '{args.ffmpeg_path}'")
            print("Please install ffmpeg or specify correct path with --ffmpeg-path")
            return

    global ffmpeg_path, overlay_mode, overlay_naming, output_dir, max_concurrent, add_exif, skip_existing, filename_prefix
    ffmpeg_path = args.ffmpeg_path
    overlay_mode = args.overlay
    overlay_naming = args.overlay_naming
    output_dir = Path(args.output)
    max_concurrent = args.concurrent
    add_exif = not args.no_exif
    skip_existing = not args.no_skip_existing
    filename_prefix = args.prefix

    json_path = Path(args.json_file)

    memories = load_memories(json_path)

    await download_all(memories)


if __name__ == "__main__":
    asyncio.run(main())
