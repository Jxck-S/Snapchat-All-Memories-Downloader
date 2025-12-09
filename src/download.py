"""Download orchestration and individual memory download handling."""

import asyncio
import time
from pathlib import Path

import httpx
from tqdm.asyncio import tqdm

from . import config
from .config import OverlayMode, OverlayNaming
from .memory import Memory, MediaType
from .stats import Stats
from .metadata import apply_metadata_and_timestamps
from .zip_processor import process_zip_with_overlays


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


def _filter_memories_to_download(memories: list[Memory], stats: Stats) -> list[Memory]:
    """Filter memories based on skip_existing setting. Updates skipped count in stats.
    
    Shows progress bar while scanning for existing files.
    """
    to_download = []
    
    if not config.skip_existing:
        # If not skipping existing, all memories are downloaded
        return memories
    
    print("Scanning for existing files...")
    for i, memory in enumerate(tqdm(memories, desc="Scanning", unit="file"), 1):
        if file_exists_in_tree(config.output_dir, memory.get_filename(occurrence=memory.occurrence)):
            stats.skipped += 1
        else:
            to_download.append(memory)
    
    # Show summary of skipped files
    if stats.skipped > 0:
        total = len(memories)
        skip_percent = (stats.skipped / total) * 100
        print(f"Skipping {stats.skipped}/{total} files ({skip_percent:.1f}%) already downloaded")
    
    return to_download


async def _process_and_update(
    memory: Memory,
    semaphore: asyncio.Semaphore,
    stats: Stats,
    start_time: float,
    progress_bar,
) -> None:
    """Download a single memory and update progress."""
    success, bytes_downloaded = await download_memory(memory, config.add_exif, semaphore, stats)
    if success:
        stats.downloaded += 1
    else:
        stats.failed += 1
    stats.mb += bytes_downloaded / 1024 / 1024

    elapsed = time.time() - start_time
    mb_per_sec = (stats.mb) / elapsed if elapsed > 0 else 0
    progress_bar.set_postfix({"MB/s": f"{mb_per_sec:.2f}"}, refresh=False)
    progress_bar.update(1)


async def download_memory(
    memory: Memory, add_exif: bool, semaphore: asyncio.Semaphore, stats: Stats
) -> tuple[bool, int]:
    async with semaphore:
        try:
            # Determine which URL to use based on overlay mode
            if config.overlay_mode in (OverlayMode.WITH, OverlayMode.BOTH):
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
                if config.overlay_mode == OverlayMode.NONE or not response.headers.get("Content-Type", "").lower().startswith("application/zip"):
                    # Calculate filename based on path
                    if config.overlay_mode == OverlayMode.BOTH and config.overlay_naming == OverlayNaming.SEPARATE_FOLDERS:
                        output_path = config.output_dir / config.WITHOUT_OVERLAYS_DIR / memory.get_filename(occurrence=memory.occurrence)
                    else:
                        output_path = config.output_dir / memory.get_filename(occurrence=memory.occurrence)        
                    output_path.write_bytes(content)
                    memory.path_without_overlay = output_path
                    
                    # Update counters
                    if memory.media_type == MediaType.IMAGE:
                        stats.total_images += 1
                        stats.images_without_overlay += 1
                    else:
                        stats.total_videos += 1
                        stats.videos_without_overlay += 1
                else:
                    # Process ZIP with overlays
                    await process_zip_with_overlays(config.output_dir, content, memory, stats)

                bytes_downloaded = len(content)
                # Apply metadata and timestamps
                apply_metadata_and_timestamps(memory, add_exif)

                # Always return success + byte count
                return True, bytes_downloaded

        except Exception as e:
            print(f"\nError downloading {memory.get_filename(occurrence=memory.occurrence)}: {e}")
            return False, 0


async def download_all(
    memories: list[Memory],
) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    # Create overlay folders if using 'both' mode with 'separate-folders' naming
    if config.overlay_mode == OverlayMode.BOTH and config.overlay_naming == OverlayNaming.SEPARATE_FOLDERS:
        with_overlays_dir = config.output_dir / config.WITH_OVERLAYS_DIR
        without_overlays_dir = config.output_dir / config.WITHOUT_OVERLAYS_DIR
        with_overlays_dir.mkdir(parents=True, exist_ok=True)
        without_overlays_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(config.max_concurrent)
    stats = Stats()
    start_time = time.time()

    # Filter memories to download
    to_download = _filter_memories_to_download(memories, stats)

    if not to_download:
        print("All files already downloaded!")
        return

    progress_bar = tqdm(
        total=len(to_download),
        desc="Downloading",
        unit="file",
        disable=False,
    )

    # Download all memories concurrently
    await asyncio.gather(
        *[_process_and_update(m, semaphore, stats, start_time, progress_bar) for m in to_download]
    )

    progress_bar.close()
    elapsed = time.time() - start_time
    # Print statistics summary
    stats.print_summary(elapsed)
