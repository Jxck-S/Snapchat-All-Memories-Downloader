"""Memory loading and filtering."""

import json
from pathlib import Path
from typing import List


class MemoriesLoader:
    """Load and filter Snapchat memories from JSON export."""
    
    def __init__(self, memories_json_path: str):
        self.memories_json_path = memories_json_path
        self.all_memories: List[dict] = []
        self.memories_without_location: List[dict] = []
    
    def load(self) -> None:
        """Load memories from JSON file and filter those without location."""
        try:
            with open(self.memories_json_path, 'r') as f:
                data = json.load(f)
            
            # Handle both "Saved Media" and direct list formats
            self.all_memories = data.get("Saved Media", data) if isinstance(data, dict) else data
            
            # Filter memories without location (0.0, 0.0 or no location field)
            for memory in self.all_memories:
                location = memory.get("Location", "")
                # Check if location is missing or is null/invalid coordinates
                if not location or "0.0, 0.0" in location or "Latitude, Longitude: ," in location:
                    self.memories_without_location.append(memory)
            
            print(f"Found {len(self.memories_without_location)} memories without location out of {len(self.all_memories)} total")
            
            if not self.memories_without_location:
                raise ValueError("No memories without location found!")
                
        except Exception as e:
            print(f"Error loading memories: {e}")
            raise
    
    def get_visible(self, location_data: dict, hide_added: bool) -> List[dict]:
        """Get visible memories based on hide_added flag.
        
        Args:
            location_data: Dict of LocationData objects keyed by timestamp
            hide_added: If True, hide memories that already have locations added
            
        Returns:
            List of visible memories
        """
        if not hide_added:
            return self.memories_without_location
        
        # Filter out memories that already have locations added or were skipped
        return [m for m in self.memories_without_location 
                if m.get("Date") not in location_data]
