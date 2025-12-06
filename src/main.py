import asyncio
import json
from pathlib import Path

from . import config
from . import args as args_module
from .memory import Memory
from .ffmpeg import check_ffmpeg
from .download import download_all




def load_memories(json_path: Path) -> list[Memory]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    memories = [Memory(**item) for item in data["Saved Media"]]
    print(f"Found {len(memories)} memories in {json_path.name}")
    return memories


async def main():
    json_path = args_module.setup_config()
    if json_path is None:
        return

    # Check ffmpeg availability
    if not check_ffmpeg(config.ffmpeg_path, config.overlay_mode):
        return

    memories = load_memories(json_path)
    await download_all(memories)



if __name__ == "__main__":
    asyncio.run(main())
