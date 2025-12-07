import asyncio
import json
from pathlib import Path
import os

from . import config
from . import args as args_module
from .memory import Memory
from .ffmpeg import check_ffmpeg
from .download import download_all




def load_memories(json_path: Path) -> tuple[dict, list[Memory]]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    memories = [Memory(**item) for item in data.get("Saved Media", [])]
    print(f"Found {len(memories)} memories in {json_path.name}")
    return data, memories


def _atomic_write_json(out_path: Path, data: dict):
    """Atomically write JSON to file to avoid corruption."""
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)


def save_memories_with_ocr(json_path: Path, original_data: dict, memories: list[Memory]) -> Path:
    """Overwrite the input JSON with OCR-enriched memories under 'extracted_ocr_text'."""
    out_path = json_path
    # Create a backup before overwriting
    backup_path = json_path.with_suffix(json_path.suffix + ".backup")
    try:
        if json_path.exists():
            json_path.replace(backup_path)
            # Move backup back to original after we finish; we'll re-write new json at original path
            backup_path.replace(json_path)
    except Exception:
        # Best-effort backup; continue even if backup fails
        pass
    # Ensure Pydantic applies json encoders (e.g., datetime -> UTC string)
    items = [m.model_dump(by_alias=True, mode="json") for m in memories]
    out_data = dict(original_data)
    out_data["Saved Media"] = items
    _atomic_write_json(out_path, out_data)
    print(f"Updated JSON with OCR captions: {out_path.name}")
    return out_path


async def main():
    json_path = args_module.setup_config()
    if json_path is None:
        return

    # Check ffmpeg availability
    if not check_ffmpeg(config.ffmpeg_path, config.overlay_mode):
        return

    original_data, memories = load_memories(json_path)
    await download_all(memories)
    # If OCR metadata was enabled, persist extracted text back into JSON
    if config.ocr_metadata:
        save_memories_with_ocr(json_path, original_data, memories)



if __name__ == "__main__":
    asyncio.run(main())
