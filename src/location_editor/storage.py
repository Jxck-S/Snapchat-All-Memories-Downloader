"""Storage handler for location data - works directly with memories JSON."""

import json
import shutil
from pathlib import Path
from typing import Dict, List, Tuple


class LocationStorage:
    """Handle loading and saving location data directly to memories JSON."""
    
    def __init__(self, memories_json_path: str):
        """Initialize storage with path to memories JSON file.
        
        Args:
            memories_json_path: Path to the memories JSON file to edit directly
        """
        self.memories_json_path = memories_json_path
        self.memories_data: dict = {}
        self.memories_list: List[dict] = []
        self.modified_timestamps: set = set()  # Track which memories were modified
        self.skipped_timestamps: set = set()   # Track which memories were skipped
    
    def load_memories(self) -> None:
        """Load memories from JSON file."""
        try:
            with open(self.memories_json_path, 'r') as f:
                self.memories_data = json.load(f)
            
            # Get the memories list
            self.memories_list = self.memories_data.get("Saved Media", self.memories_data) if isinstance(self.memories_data, dict) else self.memories_data
            print(f"Loaded {len(self.memories_list)} memories from {self.memories_json_path}")
        except Exception as e:
            print(f"Error loading memories: {e}")
            raise
    
    def set_location(self, timestamp: str, latitude: float, longitude: float) -> bool:
        """Set location for a memory by timestamp.
        
        Args:
            timestamp: Memory timestamp (Date field)
            latitude: Latitude value
            longitude: Longitude value
            
        Returns:
            True if successful, False otherwise
        """
        for memory in self.memories_list:
            if memory.get("Date") == timestamp:
                # Use underscore field names (our custom fields)
                memory["latitude"] = latitude
                memory["longitude"] = longitude
                # Also set Location field with underscore version for consistency
                memory["Location"] = f"Latitude, Longitude: {latitude}, {longitude}"
                memory["manual_location"] = True
                self.modified_timestamps.add(timestamp)
                self.skipped_timestamps.discard(timestamp)  # Remove from skipped if was there
                return True
        return False
    
    def skip_memory(self, timestamp: str) -> bool:
        """Mark a memory as skipped (no location to add).
        
        Args:
            timestamp: Memory timestamp (Date field)
            
        Returns:
            True if successful, False otherwise
        """
        self.skipped_timestamps.add(timestamp)
        self.modified_timestamps.discard(timestamp)  # Remove from modified if was there
        return True
    
    def save(self) -> Tuple[int, int]:
        """Save all changes back to the memories JSON file.
        
        Returns:
            Tuple of (locations_added, memories_skipped)
        """
        try:
            # Create backup
            backup_path = self.memories_json_path + ".backup"
            shutil.copy2(self.memories_json_path, backup_path)
            print(f"Created backup: {backup_path}")
            
            # Write updated JSON
            with open(self.memories_json_path, 'w') as f:
                json.dump(self.memories_data, f, indent=4)
            
            locations_added = len(self.modified_timestamps)
            skipped = len(self.skipped_timestamps)
            print(f"Successfully saved {locations_added} locations and skipped {skipped} memories to {self.memories_json_path}")
            return locations_added, skipped
            
        except Exception as e:
            print(f"Error saving to JSON: {e}")
            raise
    
    def get_stats(self) -> Dict[str, int]:
        """Get statistics about modifications.
        
        Returns:
            Dictionary with modification stats
        """
        return {
            "total_memories": len(self.memories_list),
            "locations_added": len(self.modified_timestamps),
            "memories_skipped": len(self.skipped_timestamps)
        }
