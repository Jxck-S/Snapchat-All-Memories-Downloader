#!/usr/bin/env python3
"""
Location Apply GUI - A tool to add location data to Snapchat memories without location info.

This GUI allows you to:
- Load memories from a JSON file
- Filter and view only memories without location data
- Display the memory media (image/video preview)
- Click on an OpenStreetMap to set the location
- Save location data to a separate JSON file
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
import io
import traceback
import threading
import time

import httpx
from PIL import Image, ImageTk, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk, messagebox
from tkintermapview import TkinterMapView

try:
    import imageio.v3 as iio
    HAS_IMAGEIO = True
except ImportError:
    HAS_IMAGEIO = False
    print("Warning: imageio not installed. Video playback not available.")
    print("Install with: pip install imageio imageio-ffmpeg")


class LocationData:
    """Store location data for a memory."""
    def __init__(self, timestamp: str, latitude: float = None, longitude: float = None, skipped: bool = False):
        self.timestamp = timestamp
        self.latitude = latitude
        self.longitude = longitude
        self.skipped = skipped
    
    def to_dict(self) -> dict:
        if self.skipped:
            return {
                "timestamp": self.timestamp,
                "skipped": True
            }
        return {
            "timestamp": self.timestamp,
            "latitude": self.latitude,
            "longitude": self.longitude
        }


class LocationEditorGUI:
    """GUI for editing memory locations directly in the memories JSON file."""
    
    def __init__(self, memories_json_path: str, output_json_path: str = "added_locations.json"):
        self.memories_json_path = memories_json_path
        self.output_json_path = output_json_path
        self.memories_data: dict = {}  # Full JSON structure
        self.memories_list: List[dict] = []  # List of memories
        self.memories_without_location: List[dict] = []
        self.current_index = 0
        self.current_media_path: Optional[Path] = None
        self.temp_dir = tempfile.mkdtemp(prefix="snapchat_location_editor_")
        self.hide_locations_added = False
        self.hide_skipped = False
        self.location_data: Dict[str, "LocationData"] = {}  # Track locations from added_locations.json
        self.modified_timestamps: set = set()  # Track which memories were modified
        self.skipped_timestamps: set = set()   # Track which memories were skipped
        
        # Initialize video player state early
        self.video_frames = None
        self.video_fps = 30
        self.current_frame_index = 0
        self.is_playing = False
        self.video_thread = None
        self.current_media_type = None
        self.play_button = None
        self.video_position_label = None
        self.media_label = None
        self.media_frame = None
        self.info_label = None
        self.video_player = None  # Not used, but keep for compatibility
        
        # Load existing location data from added_locations.json
        self.load_existing_locations()
        
        # Load memories
        self.load_memories()
        
        # Setup GUI
        self.root = tk.Tk()
        self.root.title("Snapchat Memory Location Editor")
        self.root.geometry("1600x950")
        
        # Modern color scheme
        self.colors = {
            'bg_dark': '#1a1a1a',
            'bg_medium': '#2d2d2d',
            'bg_light': '#3d3d3d',
            'accent': '#FFFC00',  # Snapchat yellow
            'text_primary': '#ffffff',
            'text_secondary': '#b0b0b0',
            'success': '#00C853',
            'border': '#404040'
        }
        
        # Configure root background
        self.root.configure(bg=self.colors['bg_dark'])
        
        self.setup_ui()
        
        # Update location counter
        self.update_location_counter()
        
        # Load first memory after a short delay to let GUI initialize
        if self.memories_without_location:
            self.root.after(100, self.load_current_memory_sync)
    
    def load_existing_locations(self):
        """Load existing location data from added_locations.json if it exists."""
        if Path(self.output_json_path).exists():
            try:
                with open(self.output_json_path, 'r') as f:
                    data = json.load(f)
                    # Simple LocationData-like storage
                    for item in data:
                        timestamp = item.get('timestamp')
                        if timestamp:
                            if item.get('skipped', False):
                                self.location_data[timestamp] = {'skipped': True}
                                self.skipped_timestamps.add(timestamp)
                            else:
                                self.location_data[timestamp] = {
                                    'latitude': item.get('latitude'),
                                    'longitude': item.get('longitude')
                                }
                                self.modified_timestamps.add(timestamp)
                print(f"Loaded {len(self.location_data)} entries from {self.output_json_path}")
            except Exception as e:
                print(f"Error loading existing locations: {e}")
        else:
            print(f"No existing location file found at {self.output_json_path}")
    
    def load_memories(self):
        """Load memories from JSON file and filter those without location."""
        try:
            with open(self.memories_json_path, 'r') as f:
                self.memories_data = json.load(f)
            
            # Handle both "Saved Media" and direct list formats
            self.memories_list = self.memories_data.get("Saved Media", self.memories_data) if isinstance(self.memories_data, dict) else self.memories_data
            
            # Filter memories without location (0.0, 0.0 or no location field)
            for memory in self.memories_list:
                location = memory.get("Location", "")
                # Check if location is missing or is null/invalid coordinates
                if not location or "0.0, 0.0" in location or "Latitude, Longitude: ," in location:
                    self.memories_without_location.append(memory)
            
            print(f"Found {len(self.memories_without_location)} memories without location out of {len(self.memories_list)} total")
            
            if not self.memories_without_location:
                messagebox.showinfo("Info", "No memories without location found!")
                sys.exit(0)
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load memories: {e}")
            sys.exit(1)
    
    def get_visible_memories(self) -> List[dict]:
        """Get the list of memories to display based on hide settings."""
        visible = self.memories_without_location
        
        # Filter out memories that have locations added if hide_locations_added is enabled
        if self.hide_locations_added:
            visible = [m for m in visible if m.get("Date") not in self.modified_timestamps]
        
        # Filter out memories that were skipped if hide_skipped is enabled
        if self.hide_skipped:
            visible = [m for m in visible if m.get("Date") not in self.skipped_timestamps]
        
        return visible
    
    def update_location_counter(self):
        """Update the counter label showing how many locations have been added."""
        locations_added = len(self.modified_timestamps)
        skipped = len(self.skipped_timestamps)
        if self.counter_info_label:
            self.counter_info_label.config(text=f"Locations added: {locations_added} | Skipped: {skipped}")

    def setup_ui(self):
        """Setup the GUI components."""
        # Configure style
        style = ttk.Style()
        style.theme_use('clam')
        
        # Configure modern dark theme styles
        style.configure('TFrame', background=self.colors['bg_dark'])
        style.configure('TLabel', background=self.colors['bg_dark'], foreground=self.colors['text_primary'], 
                       font=('SF Pro Display', 11))
        style.configure('TButton', background=self.colors['bg_light'], foreground=self.colors['text_primary'],
                       font=('SF Pro Display', 10, 'bold'), borderwidth=0, focuscolor='none')
        style.map('TButton', background=[('active', self.colors['accent'])])
        style.configure('TEntry', fieldbackground=self.colors['bg_light'], foreground=self.colors['text_primary'],
                       borderwidth=1, insertcolor=self.colors['text_primary'])
        style.configure('TCheckbutton', background=self.colors['bg_dark'], foreground=self.colors['text_secondary'],
                       font=('SF Pro Display', 10))
        
        # Main container (scrollable)
        container = tk.Frame(self.root, bg=self.colors['bg_dark'])
        container.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Canvas + vertical scrollbar
        canvas = tk.Canvas(container, bg=self.colors['bg_dark'], highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        vscroll.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # Frame inside canvas acts as the real content root
        main_frame = ttk.Frame(canvas, padding="20")
        canvas_window = canvas.create_window((0, 0), window=main_frame, anchor="nw")

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        def _resize_content(event):
            # Keep inner frame width synced to canvas width
            canvas.itemconfigure(canvas_window, width=canvas.winfo_width())
            canvas.configure(scrollregion=canvas.bbox("all"))

        # Update scrollregion when content changes or window resizes
        main_frame.bind("<Configure>", _resize_content)
        canvas.bind("<Configure>", _resize_content)

        # Enable mouse wheel scrolling on the canvas
        def _on_scrollwheel(e):
            # macOS event.delta is small/negative for down; scale a bit
            delta = -1 if e.delta < 0 else 1
            canvas.yview_scroll(delta * 3, "units")

        canvas.bind("<MouseWheel>", _on_scrollwheel)

        # Keep content columns flexible
        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=2)
        main_frame.rowconfigure(1, weight=1)
        
        # Left side - Media preview card
        left_frame = tk.Frame(main_frame, bg=self.colors['bg_medium'], highlightthickness=0)
        left_frame.grid(row=0, column=0, rowspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 20))
        
        # Header section
        header_frame = tk.Frame(left_frame, bg=self.colors['bg_medium'])
        header_frame.pack(pady=(15, 10), padx=15, fill=tk.X)
        
        # Info label with modern styling
        self.info_label = tk.Label(header_frame, text="Loading...", font=('SF Pro Display', 12), 
                                   bg=self.colors['bg_medium'], fg=self.colors['text_primary'], anchor='w')
        self.info_label.pack(pady=5, fill=tk.X)
        
        # Counter label with modern accent color
        self.counter_info_label = tk.Label(header_frame, text="Locations added: 0", 
                                          font=('SF Pro Display', 11, 'bold'), 
                                          bg=self.colors['bg_medium'], fg=self.colors['success'], anchor='w')
        self.counter_info_label.pack(pady=2, fill=tk.X)
        
        # Checkbox with modern styling
        checkbox_frame = tk.Frame(header_frame, bg=self.colors['bg_medium'])
        checkbox_frame.pack(pady=8, fill=tk.X)
        
        # Checkbox 1: Hide locations added
        self.hide_locations_added_var = tk.BooleanVar(value=False)
        self.hide_locations_added_checkbox = tk.Checkbutton(
            checkbox_frame,
            text="Hide locations added",
            variable=self.hide_locations_added_var,
            command=self.toggle_hide_filters,
            bg=self.colors['bg_medium'],
            fg=self.colors['text_secondary'],
            selectcolor=self.colors['bg_light'],
            activebackground=self.colors['bg_medium'],
            activeforeground=self.colors['accent'],
            font=('SF Pro Display', 10),
            highlightthickness=0,
            bd=0
        )
        self.hide_locations_added_checkbox.pack(anchor='w')
        
        # Checkbox 2: Hide skipped
        self.hide_skipped_var = tk.BooleanVar(value=False)
        self.hide_skipped_checkbox = tk.Checkbutton(
            checkbox_frame,
            text="Hide skipped",
            variable=self.hide_skipped_var,
            command=self.toggle_hide_filters,
            bg=self.colors['bg_medium'],
            fg=self.colors['text_secondary'],
            selectcolor=self.colors['bg_light'],
            activebackground=self.colors['bg_medium'],
            activeforeground=self.colors['accent'],
            font=('SF Pro Display', 10),
            highlightthickness=0,
            bd=0
        )
        self.hide_skipped_checkbox.pack(anchor='w')
        
        # Media container with shadow effect
        media_container = tk.Frame(left_frame, bg=self.colors['bg_medium'])
        media_container.pack(expand=False, pady=10)
        
        # Media frame - Snapchat memory dimensions (9:16 aspect ratio)
        self.media_frame = tk.Frame(media_container, bg='#000000', relief=tk.FLAT, bd=0, 
                                    highlightbackground=self.colors['border'], highlightthickness=1)
        self.media_frame.pack(padx=15)
        # Configure frame to maintain minimum size
        self.media_frame.grid_propagate(False)
        self.media_frame.pack_propagate(False)
        # Snapchat memory size: 1080x1920 scaled down (scale ~0.3 = 324x576)
        self.media_frame.config(width=324, height=576)
        
        # Media display label (for images)
        self.media_label = tk.Label(self.media_frame, text="Media will appear here", 
                                    bg='#000000', fg=self.colors['text_secondary'], 
                                    compound=tk.CENTER, font=('SF Pro Display', 11))
        self.media_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        
        # Video controls with modern styling
        controls_container = tk.Frame(left_frame, bg=self.colors['bg_medium'], height=50)
        controls_container.pack(pady=10, fill=tk.X, padx=15)
        controls_container.pack_propagate(False)
        
        self.video_controls_frame = tk.Frame(controls_container, bg=self.colors['bg_light'], height=40)
        self.video_controls_frame.pack(expand=True, fill=tk.BOTH)
        self.video_controls_frame.pack_propagate(False)
        
        self.play_button = tk.Button(self.video_controls_frame, text="▶ Play", command=self.toggle_video_play, 
                                     state=tk.DISABLED, bg=self.colors['accent'], fg='#000000',
                                     font=('SF Pro Display', 10, 'bold'), relief=tk.FLAT, 
                                     activebackground=self.colors['accent'], cursor='hand2', bd=0)
        self.play_button.pack(side=tk.LEFT, padx=10, pady=5)
        
        self.video_position_label = tk.Label(self.video_controls_frame, text="", 
                                            font=('SF Pro Display', 9), bg=self.colors['bg_light'],
                                            fg=self.colors['text_secondary'])
        self.video_position_label.pack(side=tk.LEFT, padx=10)
        
        # Navigation buttons with modern styling
        nav_frame = tk.Frame(left_frame, bg=self.colors['bg_medium'])
        nav_frame.pack(pady=15, padx=15, fill=tk.X)
        
        self.prev_button = tk.Button(nav_frame, text="← Previous", command=self.previous_memory,
                                     bg=self.colors['bg_light'], fg=self.colors['text_primary'],
                                     font=('SF Pro Display', 10, 'bold'), relief=tk.FLAT,
                                     activebackground=self.colors['accent'], activeforeground='#000000',
                                     cursor='hand2', padx=20, pady=8, bd=0)
        self.prev_button.pack(side=tk.LEFT, padx=5)
        
        self.counter_label = tk.Label(nav_frame, text="0/0", font=('SF Pro Display', 14, 'bold'),
                                     bg=self.colors['bg_medium'], fg=self.colors['accent'])
        self.counter_label.pack(side=tk.LEFT, padx=20)
        
        self.next_button = tk.Button(nav_frame, text="Next →", command=self.next_memory,
                                    bg=self.colors['bg_light'], fg=self.colors['text_primary'],
                                    font=('SF Pro Display', 10, 'bold'), relief=tk.FLAT,
                                    activebackground=self.colors['accent'], activeforeground='#000000',
                                    cursor='hand2', padx=20, pady=8, bd=0)
        self.next_button.pack(side=tk.LEFT, padx=5)
        
        # Export button
        export_frame = tk.Frame(left_frame, bg=self.colors['bg_medium'])
        export_frame.pack(pady=10, padx=15, fill=tk.X)
        
        self.export_button = tk.Button(export_frame, text="💾 Export to JSON", 
                                       command=self.export_to_original_json,
                                       bg=self.colors['accent'], fg='#000000',
                                       font=('SF Pro Display', 11, 'bold'), relief=tk.FLAT,
                                       activebackground=self.colors['accent'], cursor='hand2',
                                       padx=20, pady=12, bd=0)
        self.export_button.pack(fill=tk.X)
        
        # Right side - Map and controls card
        right_frame = tk.Frame(main_frame, bg=self.colors['bg_medium'], highlightthickness=0)
        right_frame.grid(row=0, column=1, rowspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))
        right_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(1, weight=1)
        
        # Map controls
        control_frame = tk.Frame(right_frame, bg=self.colors['bg_medium'])
        control_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(15, 15), padx=15)
        control_frame.columnconfigure(1, weight=1)
        
        # Search section with modern styling
        search_label = tk.Label(control_frame, text="Search Location:", font=('SF Pro Display', 11, 'bold'),
                               bg=self.colors['bg_medium'], fg=self.colors['text_primary'])
        search_label.grid(row=0, column=0, padx=(0, 10), sticky=tk.W)
        
        self.search_entry = tk.Entry(control_frame, font=('SF Pro Display', 11),
                                     bg=self.colors['bg_light'], fg=self.colors['text_primary'],
                                     insertbackground=self.colors['accent'], relief=tk.FLAT,
                                     highlightthickness=1, highlightbackground=self.colors['border'],
                                     highlightcolor=self.colors['accent'])
        self.search_entry.grid(row=0, column=1, padx=5, sticky=(tk.W, tk.E), ipady=8)
        self.search_entry.bind('<Return>', lambda e: self.search_location())
        
        search_button = tk.Button(control_frame, text="🔍 Search", command=self.search_location,
                                 bg=self.colors['accent'], fg='#000000', font=('SF Pro Display', 10, 'bold'),
                                 relief=tk.FLAT, activebackground=self.colors['accent'],
                                 cursor='hand2', padx=15, pady=8, bd=0)
        search_button.grid(row=0, column=2, padx=5)
        
        # Location controls with modern styling
        tk.Label(control_frame, text="Click map to set location:", font=('SF Pro Display', 10),
                bg=self.colors['bg_medium'], fg=self.colors['text_secondary']).grid(row=1, column=0, columnspan=2, padx=0, pady=(15, 5), sticky=tk.W)
        
        self.coords_label = tk.Label(control_frame, text="Lat: -, Lon: -", font=('SF Pro Display', 11, 'bold'),
                                     bg=self.colors['bg_medium'], fg=self.colors['accent'])
        self.coords_label.grid(row=2, column=0, columnspan=2, padx=0, sticky=tk.W)
        
        button_frame = tk.Frame(control_frame, bg=self.colors['bg_medium'])
        button_frame.grid(row=1, column=2, rowspan=2, padx=5)
        
        self.save_button = tk.Button(button_frame, text="💾 Save", command=self.save_current_location, 
                                     state=tk.DISABLED, bg=self.colors['success'], fg='#ffffff',
                                     font=('SF Pro Display', 10, 'bold'), relief=tk.FLAT,
                                     activebackground=self.colors['success'], cursor='hand2',
                                     padx=15, pady=8, bd=0)
        self.save_button.pack(side=tk.TOP, pady=3)
        
        self.skip_button = tk.Button(button_frame, text="⏭ Skip", command=self.skip_current_memory,
                                     bg=self.colors['bg_light'], fg=self.colors['text_primary'],
                                     font=('SF Pro Display', 9), relief=tk.FLAT,
                                     activebackground=self.colors['accent'], activeforeground='#000000',
                                     cursor='hand2', padx=15, pady=6, bd=0)
        self.skip_button.pack(side=tk.TOP, pady=3)
        
        self.clear_button = tk.Button(button_frame, text="Clear", command=self.clear_marker,
                                      bg=self.colors['bg_light'], fg=self.colors['text_secondary'],
                                      font=('SF Pro Display', 9), relief=tk.FLAT,
                                      activebackground='#ff4444', activeforeground='#ffffff',
                                      cursor='hand2', padx=15, pady=6, bd=0)
        self.clear_button.pack(side=tk.TOP, pady=3)
        
        # Map view with all mouse/trackpad features enabled
        self.map_widget = TkinterMapView(right_frame, corner_radius=0)
        self.map_widget.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.map_widget.set_tile_server("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")
        
        # Set default position (center of US)
        self.map_widget.set_position(39.8283, -98.5795)
        self.map_widget.set_zoom(4)
        
        # Enable mouse wheel zoom explicitly for macOS trackpad
        # Bind mousewheel events for both vertical and horizontal scrolling
        if sys.platform == "darwin":  # macOS
            self.map_widget.bind("<MouseWheel>", self._on_mousewheel)
            self.map_widget.bind("<Shift-MouseWheel>", self._on_mousewheel)
        else:  # Windows/Linux
            self.map_widget.bind("<MouseWheel>", self._on_mousewheel)
            self.map_widget.bind("<Button-4>", self._on_mousewheel)
            self.map_widget.bind("<Button-5>", self._on_mousewheel)
        
        # Add click event for setting location
        self.map_widget.add_left_click_map_command(self.map_click_event)
        
        self.current_marker = None
        self.selected_lat = None
        self.selected_lon = None
    
    def _on_mousewheel(self, event):
        """Handle mousewheel/trackpad zoom events."""
        try:
            # Get current zoom level
            current_zoom = self.map_widget.zoom
            
            # Determine zoom direction based on event delta
            if event.num == 5 or event.delta < 0:
                # Zoom out
                new_zoom = max(0, current_zoom - 1)
            else:
                # Zoom in
                new_zoom = min(19, current_zoom + 1)
            
            # Apply new zoom
            if new_zoom != current_zoom:
                self.map_widget.set_zoom(new_zoom)
        except Exception as e:
            print(f"Error handling zoom: {e}")
    
    def map_click_event(self, coords):
        """Handle map click event."""
        lat, lon = coords
        self.selected_lat = lat
        self.selected_lon = lon
        
        # Update coordinates label
        self.coords_label.config(text=f"Lat: {lat:.6f}, Lon: {lon:.6f}")
        
        # Remove old marker if exists
        if self.current_marker:
            self.current_marker.delete()
        
        # Add new marker with Snapchat yellow (modern clean look)
        self.current_marker = self.map_widget.set_marker(lat, lon, text="", 
                                                          marker_color_circle="#FFFC00",
                                                          marker_color_outside="#FFD700")
        
        # Enable save button
        self.save_button.config(state=tk.NORMAL)
    
    def clear_marker(self):
        """Clear the current marker."""
        if self.current_marker:
            self.current_marker.delete()
            self.current_marker = None
        self.selected_lat = None
        self.selected_lon = None
        self.coords_label.config(text="Lat: -, Lon: -")
        self.save_button.config(state=tk.DISABLED)
    
    def search_location(self):
        """Search for a location by name using Nominatim (OpenStreetMap)."""
        query = self.search_entry.get().strip()
        if not query:
            return
        
        try:
            # Use Nominatim API (OpenStreetMap's geocoding service)
            url = "https://nominatim.openstreetmap.org/search"
            params = {
                "q": query,
                "format": "json",
                "limit": 1
            }
            headers = {
                "User-Agent": "SnapchatMemoryLocationEditor/1.0"
            }
            
            print(f"Searching for location: {query}")
            
            # Make synchronous request using httpx
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                results = response.json()
            
            if results:
                result = results[0]
                lat = float(result['lat'])
                lon = float(result['lon'])
                display_name = result.get('display_name', query)
                
                print(f"Found: {display_name} at {lat}, {lon}")
                
                # Jump to location on map
                self.map_widget.set_position(lat, lon)
                self.map_widget.set_zoom(15)  # Zoom in to street level
                
                # Optionally set marker at searched location
                # (User can adjust by clicking on map)
                messagebox.showinfo("Location Found", f"Found: {display_name}\n\nClick on the map to set the exact location.")
                
            else:
                messagebox.showwarning("Not Found", f"No results found for '{query}'.\n\nTry a different search term.")
                
        except Exception as e:
            print(f"Error searching location: {e}")
            traceback.print_exc()
            messagebox.showerror("Search Error", f"Failed to search location:\n{e}")
    
    async def download_media(self, url: str, media_type: str) -> Optional[Path]:
        """Download media from CDN URL."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: POST to Snapchat endpoint to get actual CDN URL
                print(f"Getting CDN URL from: {url[:50]}...")
                response = await client.post(
                    url,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                cdn_url = response.text.strip()
                print(f"CDN URL received: {cdn_url[:50]}...")
                
                # Step 2: Download from the actual CDN URL
                print(f"Downloading media from CDN...")
                response = await client.get(cdn_url, follow_redirects=True)
                response.raise_for_status()
                
                print(f"Downloaded {len(response.content)} bytes")
                
                # Save to temp file
                ext = ".jpg" if media_type.lower() == "image" else ".mp4"
                temp_file = Path(self.temp_dir) / f"current_media{ext}"
                temp_file.write_bytes(response.content)
                
                print(f"Saved to: {temp_file}")
                return temp_file
        except Exception as e:
            print(f"Error downloading media: {e}")
            traceback.print_exc()
            return None
    
    def load_video_frames(self, video_path: Path):
        """Load video frames using imageio."""
        try:
            print(f"Loading video frames: {video_path}")
            
            # Check file size first
            file_size = video_path.stat().st_size
            if file_size < 1000:
                print(f"Video file too small ({file_size} bytes), likely corrupted or invalid")
                return False
            
            # Try different plugins in order of preference
            plugin = None
            for plugin_name in ["ffmpeg", "pyav"]:
                try:
                    props = iio.improps(str(video_path), plugin=plugin_name)
                    plugin = plugin_name
                    self.video_fps = props.fps if hasattr(props, 'fps') and props.fps > 0 else 30
                    print(f"Using {plugin_name} plugin, fps: {self.video_fps}")
                    break
                except Exception as e:
                    print(f"Failed to use {plugin_name} plugin: {e}")
                    continue
            
            if plugin is None:
                print("No suitable video plugin available")
                return False
            
            # Read all frames (for short videos) or sample frames
            frames = []
            for i, frame in enumerate(iio.imiter(str(video_path), plugin=plugin)):
                # Convert to PIL Image
                img = Image.fromarray(frame)
                frames.append(img)
                
                # Limit to first 300 frames to avoid memory issues
                if i >= 299:
                    print(f"Loaded first 300 frames (fps: {self.video_fps})")
                    break
            
            if not frames:
                print("No frames could be extracted from video")
                return False
            
            self.video_frames = frames
            self.current_frame_index = 0
            
            print(f"Video loaded: {len(frames)} frames at {self.video_fps} fps")
            
            # Display first frame
            if frames:
                self.display_video_frame(frames[0])
            
            return True
            
        except Exception as e:
            print(f"Error loading video: {e}")
            traceback.print_exc()
            return False
    
    def display_video_frame(self, frame: Image.Image):
        """Display a single video frame."""
        try:
            # Resize frame
            frame_copy = frame.copy()
            frame_copy.thumbnail((600, 600), Image.Resampling.LANCZOS)
            
            # Convert to PhotoImage
            photo = ImageTk.PhotoImage(frame_copy)
            self.media_label.config(image=photo, text="")
            self.media_label.image = photo
            
        except Exception as e:
            print(f"Error displaying frame: {e}")
    
    def play_video_loop(self):
        """Video playback loop (runs in separate thread)."""
        try:
            frame_delay = 1.0 / self.video_fps
            
            while self.is_playing and self.video_frames:
                # Display current frame
                frame = self.video_frames[self.current_frame_index]
                self.display_video_frame(frame)
                
                # Update position label
                if self.video_position_label:
                    self.video_position_label.config(
                        text=f"Frame {self.current_frame_index + 1}/{len(self.video_frames)}"
                    )
                
                # Next frame
                self.current_frame_index += 1
                if self.current_frame_index >= len(self.video_frames):
                    self.current_frame_index = 0  # Loop
                
                time.sleep(frame_delay)
                
        except Exception as e:
            print(f"Error in video playback: {e}")
            traceback.print_exc()
            self.is_playing = False
    
    def stop_video(self):
        """Stop video playback and clean up."""
        self.is_playing = False
        if self.video_thread and self.video_thread.is_alive():
            self.video_thread.join(timeout=1.0)
        self.video_thread = None
        self.current_frame_index = 0
    
    def toggle_video_play(self):
        """Toggle video play/pause."""
        if not HAS_IMAGEIO:
            messagebox.showwarning("Video Player Required", 
                "imageio is required for video playback.\nInstall with: pip install imageio imageio-ffmpeg")
            return
        
        if not self.video_frames:
            return
        
        if self.is_playing:
            # Pause
            self.is_playing = False
            if self.play_button:
                self.play_button.config(text="▶ Play")
        else:
            # Play
            self.is_playing = True
            if self.play_button:
                self.play_button.config(text="⏸ Pause")
            
            # Start playback thread
            if not self.video_thread or not self.video_thread.is_alive():
                self.video_thread = threading.Thread(target=self.play_video_loop, daemon=True)
                self.video_thread.start()
    
    def create_placeholder_image(self, text: str, media_path: Path) -> Image.Image:
        """Create a placeholder image for videos that can't be previewed."""
        placeholder = Image.new('RGB', (600, 400), color=(44, 62, 80))
        draw = ImageDraw.Draw(placeholder)
        
        # Draw rectangles and text
        draw.rectangle([50, 50, 550, 350], outline=(255, 255, 255), width=3)
        
        # Draw video icon (simple play button triangle)
        draw.polygon([(250, 150), (250, 250), (350, 200)], fill=(255, 255, 255))
        
        # Draw text without emoji
        draw.text((200, 280), "VIDEO FILE", fill=(255, 255, 255))
        draw.text((150, 320), f"{media_path.name[:40]}", fill=(149, 165, 166))
        
        print("Placeholder image created successfully")
        return placeholder
    
    def display_media(self, media_path: Path, media_type: str):
        """Display media in the GUI."""
        try:
            # Stop any existing video playback
            self.stop_video()
            
            # Clear previous content to prevent flashing
            self.media_label.image = None
            
            print(f"\n{'='*60}")
            print(f"Displaying media: {media_path} (type: {media_type})")
            print(f"File exists: {media_path.exists()}, Size: {media_path.stat().st_size if media_path.exists() else 0} bytes")
            
            self.current_media_type = media_type.lower()
            
            if self.current_media_type == "image":
                # Display image
                print("Loading image...")
                
                img = Image.open(media_path)
                print(f"Image loaded: {img.size}, mode: {img.mode}")
                
                # Convert to RGB if necessary
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                    print(f"Converted to RGB")
                
                # Resize to fit in display area (max 600x600) while maintaining aspect ratio
                img.thumbnail((600, 600), Image.Resampling.LANCZOS)
                print(f"Image resized to: {img.size}")
                
                # Ensure image is in RGB mode before converting to PhotoImage
                if img.mode not in ('RGB', 'RGBA', 'L'):
                    print(f"Converting {img.mode} to RGB...")
                    img = img.convert('RGB')
                
                print(f"Creating PhotoImage from PIL Image: {img.size}, mode: {img.mode}")
                
                # Convert to PhotoImage and display
                photo = ImageTk.PhotoImage(img)
                print(f"PhotoImage created: {photo.width()}x{photo.height()}")
                
                self.media_label.config(image=photo, text="", compound=tk.CENTER)
                self.media_label.image = photo  # Keep a reference to prevent garbage collection
                
                # Disable video controls for images
                if self.play_button:
                    self.play_button.config(state=tk.DISABLED)
                if self.video_position_label:
                    self.video_position_label.config(text="")
                
                print("Image displayed successfully")
                
            else:  # video
                print("Loading video...")
                
                # Make sure image label is visible
                self.media_label.pack(expand=True, fill=tk.BOTH)
                
                # Load and play video
                if HAS_IMAGEIO:
                    success = self.load_video_frames(media_path)
                    if success:
                        if self.play_button:
                            self.play_button.config(state=tk.NORMAL, text="> Play")
                        if self.video_position_label:
                            self.video_position_label.config(text=f"Ready ({len(self.video_frames)} frames)")
                        print("[OK] Video loaded successfully")
                    else:
                        self.media_label.config(text=f"Video: {media_path.name}\n\nFailed to load video", compound=tk.CENTER)
                        if self.play_button:
                            self.play_button.config(state=tk.DISABLED)
                else:
                    # Show fallback message
                    self.media_label.config(text=f"Video: {media_path.name}\n\nInstall imageio to play videos:\npip install imageio imageio-ffmpeg", compound=tk.CENTER)
                    if self.play_button:
                        self.play_button.config(state=tk.DISABLED)
                    if self.video_position_label:
                        self.video_position_label.config(text="Not available")
            
            print("[OK] Media displayed successfully")
            print(f"{'='*60}\n")
                
        except Exception as e:
            error_msg = f"Error displaying media: {e}"
            print(error_msg)
            traceback.print_exc()
            if self.media_label:
                self.media_label.config(text=error_msg, image="")
            if self.play_button:
                self.play_button.config(state=tk.DISABLED)
            if self.video_position_label:
                self.video_position_label.config(text="")
    
    async def load_current_memory(self):
        """Load the current memory's media and update UI."""
        if not self.memories_without_location:
            return
        
        memory = self.memories_without_location[self.current_index]
        date = memory.get("Date", "Unknown")
        media_type = memory.get("Media Type", "Unknown")
        download_link = memory.get("Download Link", "")
        
        # Update info
        visible_memories = self.get_visible_memories()
        visible_index = visible_memories.index(memory) if memory in visible_memories else 0
        self.info_label.config(text=f"Date: {date}\nType: {media_type}")
        self.counter_label.config(text=f"{visible_index + 1}/{len(visible_memories)}")
        
        # Update navigation buttons
        self.prev_button.config(state=tk.NORMAL if self.current_index > 0 else tk.DISABLED)
        self.next_button.config(state=tk.NORMAL if self.current_index < len(self.memories_without_location) - 1 else tk.DISABLED)
        
        # Clear previous marker
        self.clear_marker()
        
        # Check if location already set for this memory (from previous edits in this session)
        if date in self.modified_timestamps:
            memory = self.memories_without_location[self.current_index]
            if memory.get("latitude") and memory.get("longitude"):
                self.selected_lat = memory.get("latitude")
                self.selected_lon = memory.get("longitude")
                self.coords_label.config(text=f"Lat: {self.selected_lat:.6f}, Lon: {self.selected_lon:.6f} (SAVED)")
                self.current_marker = self.map_widget.set_marker(self.selected_lat, self.selected_lon, text="",
                                                                  marker_color_circle="#FFFC00",
                                                                  marker_color_outside="#FFD700")
                self.map_widget.set_position(self.selected_lat, self.selected_lon)
                self.map_widget.set_zoom(13)
                self.save_button.config(state=tk.NORMAL)
        elif date in self.skipped_timestamps:
            self.coords_label.config(text="SKIPPED - No location needed")
        
        # Download and display media
        # Clear image reference first to prevent layout jump
        self.media_label.image = None
        self.media_label.config(image="", text="Downloading media...", compound=tk.CENTER)
        self.root.update_idletasks()  # Use update_idletasks instead of update for smoother UI
        
        media_path = await self.download_media(download_link, media_type)
        
        if media_path:
            self.current_media_path = media_path
            self.display_media(media_path, media_type)
        else:
            self.media_label.config(text=f"Failed to download media\n{media_type}", image="", compound=tk.CENTER)
    
    def save_current_location(self):
        """Save the current location for the current memory to added_locations.json."""
        if self.selected_lat is None or self.selected_lon is None:
            messagebox.showwarning("Warning", "Please select a location on the map first!")
            return
        
        memory = self.memories_without_location[self.current_index]
        timestamp = memory.get("Date")
        
        if not timestamp:
            messagebox.showerror("Error", "Memory has no timestamp!")
            return
        
        # Store location data in added_locations.json format
        self.location_data[timestamp] = {
            'latitude': self.selected_lat,
            'longitude': self.selected_lon
        }
        
        # Track modification and clear skip status
        self.modified_timestamps.add(timestamp)
        self.skipped_timestamps.discard(timestamp)
        
        # Save to added_locations.json
        self.save_locations_to_file()
        
        # Update counter
        self.update_location_counter()
        
        # Update UI
        self.coords_label.config(text=f"Lat: {self.selected_lat:.6f}, Lon: {self.selected_lon:.6f} (SAVED)")
        
        # If hide_locations_added is enabled, automatically move to next memory without added location
        if self.hide_locations_added:
            visible_memories = self.get_visible_memories()
            if not visible_memories:
                messagebox.showinfo("All Done!", f"Location saved for memory at {timestamp}\n\nAll memories now have locations added!")
            else:
                messagebox.showinfo("Success", f"Location saved for memory at {timestamp}\n\nMoving to next memory...")
                self.next_memory()
        else:
            messagebox.showinfo("Success", f"Location saved for memory at {timestamp}")
    
    def skip_current_memory(self):
        """Mark current memory as not needing location (e.g., downloaded video)."""
        memory = self.memories_without_location[self.current_index]
        timestamp = memory.get("Date")
        
        if not timestamp:
            messagebox.showerror("Error", "Memory has no timestamp!")
            return
        
        # Store as skipped entry
        self.location_data[timestamp] = {'skipped': True}
        
        # Track as skipped
        self.skipped_timestamps.add(timestamp)
        self.modified_timestamps.discard(timestamp)
        
        # Save to added_locations.json
        self.save_locations_to_file()
        
        # Update counter
        self.update_location_counter()
        
        # If hide_skipped is enabled, automatically move to next memory
        if self.hide_skipped:
            visible_memories = self.get_visible_memories()
            if not visible_memories:
                messagebox.showinfo("All Done!", f"Memory at {timestamp} skipped (no location needed).\n\nAll memories have been processed!")
            else:
                messagebox.showinfo("Skipped", f"Memory at {timestamp} skipped.\n\nMoving to next memory...")
                self.next_memory()
        else:
            messagebox.showinfo("Skipped", f"Memory at {timestamp} marked as not needing location.")
    
    def toggle_hide_filters(self):
        """Toggle hiding memories based on filter settings."""
        self.hide_locations_added = self.hide_locations_added_var.get()
        self.hide_skipped = self.hide_skipped_var.get()
        visible_memories = self.get_visible_memories()
        
        if not visible_memories:
            messagebox.showinfo("No Matches", "No memories match the current filter settings.\n\nAdjust filters to continue.")
            return
        
        # If current memory is now hidden, move to next visible one
        current_memory = self.memories_without_location[self.current_index]
        if current_memory not in visible_memories:
            # Find next visible memory
            for i in range(self.current_index, len(self.memories_without_location)):
                if self.memories_without_location[i] in visible_memories:
                    self.current_index = i
                    break
            else:
                # If no visible memory after current, try before
                for i in range(self.current_index - 1, -1, -1):
                    if self.memories_without_location[i] in visible_memories:
                        self.current_index = i
                        break
        
        # Reload current memory to update UI
        self.load_current_memory_sync()
    
    def save_locations_to_file(self):
        """Save all location data to added_locations.json."""
        try:
            data = []
            for timestamp, loc_data in self.location_data.items():
                if loc_data.get('skipped', False):
                    data.append({
                        "timestamp": timestamp,
                        "skipped": True
                    })
                else:
                    data.append({
                        "timestamp": timestamp,
                        "latitude": loc_data['latitude'],
                        "longitude": loc_data['longitude']
                    })
            
            with open(self.output_json_path, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"Saved {len(data)} locations to {self.output_json_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save locations: {e}")
            print(f"Error saving to {self.output_json_path}: {e}")
    
    def export_to_original_json(self):
        """Export added locations to a new copy of the memories JSON file with new format."""
        try:
            import shutil
            # Use the editor-specific relaxed Memory model
            from .memory_model import EditorMemory
            # No hardcoded alias maps; rely on model aliases and original fields
            
            # Create output filename: memories_history.json -> memories_history_locations_added.json
            json_path = Path(self.memories_json_path)
            output_path = json_path.parent / (json_path.stem + "_locations_added.json")
            
            # Load original JSON
            with open(self.memories_json_path, 'r') as f:
                data = json.load(f)
            
            # Get the memories list
            memories = data.get("Saved Media", data) if isinstance(data, dict) else data
            
            # Count of updated memories
            updated_count = 0
            
            # Update memories with added locations (skip skipped ones)
            for memory in memories:
                timestamp = memory.get("Date")
                if timestamp and timestamp in self.location_data:
                    loc = self.location_data[timestamp]
                    # Only add if not skipped
                    if not loc.get('skipped', False):
                        memory["latitude"] = loc['latitude']
                        memory["longitude"] = loc['longitude']
                        memory["manual_location"] = True
                        updated_count += 1
            
            # Normalize keys to snake_case and exclude fields we don't want from GUI export
            serialized_memories = []
            # Single path: tolerant model serialization using original Snapchat keys
            for item in memories:
                m = EditorMemory(**item)
                out = m.to_serialized_dict()
                serialized_memories.append(out)
            
            # Update the data structure
            if isinstance(data, dict):
                data["Saved Media"] = serialized_memories
            else:
                data = serialized_memories
            
            # Write to new file with new format
            with open(output_path, 'w') as f:
                json.dump(data, f, indent=4)
            
            messagebox.showinfo(
                "Export Successful", 
                f"Updated {updated_count} memories in {output_path.name}\n\n"
                f"Format: Using underscore field names (media_type, media_download_url, etc.)\n"
                f"Skipped {len(self.skipped_timestamps)} memories marked as 'no location needed'"
            )
            print(f"Successfully exported {updated_count} locations to {output_path}")
            
        except Exception as e:
            print(f"Error exporting to JSON: {e}")
            import traceback
            traceback.print_exc()
            messagebox.showerror("Export Error", f"Failed to export locations:\n{e}")
    
    
    def next_memory(self):
        """Navigate to next memory."""
        self.stop_video()
        visible_memories = self.get_visible_memories()
        
        # Find next visible memory
        for i in range(self.current_index + 1, len(self.memories_without_location)):
            if self.memories_without_location[i] in visible_memories:
                self.current_index = i
                self.load_current_memory_sync()
                return
    
    def previous_memory(self):
        """Navigate to previous memory."""
        self.stop_video()
        visible_memories = self.get_visible_memories()
        
        # Find previous visible memory
        for i in range(self.current_index - 1, -1, -1):
            if self.memories_without_location[i] in visible_memories:
                self.current_index = i
                self.load_current_memory_sync()
                return
    
    def load_current_memory_sync(self):
        """Synchronous wrapper for load_current_memory."""
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.load_current_memory())
            loop.close()
        except Exception as e:
            print(f"Error loading memory: {e}")
            traceback.print_exc()
    
    def run(self):
        """Start the GUI main loop."""
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.mainloop()
    
    def on_closing(self):
        """Handle window closing."""
        # Stop video playback
        self.stop_video()
        
        # Clean up temp directory
        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass
        
        self.root.destroy()


def main():
    """Main entry point for the location editor GUI."""
    parser = argparse.ArgumentParser(
        description="Location Apply GUI - Add location data to Snapchat memories"
    )
    parser.add_argument(
        "json_file",
        help="Path to the memories JSON file (e.g., memories_history.json)"
    )
    parser.add_argument(
        "-o", "--output",
        default="added_locations.json",
        help="Output JSON file for location data (default: added_locations.json)"
    )
    
    args = parser.parse_args()
    
    # Verify input file exists
    if not Path(args.json_file).exists():
        print(f"Error: File '{args.json_file}' not found!")
        sys.exit(1)
    
    # Create and run GUI
    app = LocationEditorGUI(args.json_file, args.output)
    app.run()


if __name__ == "__main__":
    main()
