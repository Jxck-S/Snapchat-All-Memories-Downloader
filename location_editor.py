#!/usr/bin/env python3
"""Wrapper script to launch the Location Editor GUI.

Usage:
    python location_editor.py <memories_json_file> [-o output_file]

This tool allows you to manually add location data to Snapchat memories
that don't have geolocation information.
"""

from src.location_editor import main
import asyncio

if __name__ == "__main__":
    main()
