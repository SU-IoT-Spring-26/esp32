#!/usr/bin/env python3
"""
Occupancy Estimation from Thermal Camera Data
Analyzes thermal images from MLX90640 to detect and count people in a room
"""

import requests
import numpy as np
from scipy.ndimage import label
import json
import time
from datetime import datetime

# Configuration
API_URL = "http://localhost:5000/api/thermal"  # API server endpoint
UPDATE_INTERVAL = 3.0  # How often to fetch new thermal data (seconds)

# Occupancy detection parameters
MIN_HUMAN_TEMP = 30.0  # Minimum temperature to consider as human (°C)
MAX_HUMAN_TEMP = 45.0  # Maximum temperature to consider as human (°C)
MIN_CLUSTER_SIZE = 3   # Minimum number of connected pixels to count as a person
MAX_CLUSTER_SIZE = 200 # Maximum cluster size (to filter out large hot objects)

# Room temperature estimation
ROOM_TEMP_THRESHOLD = 0.5  # Temperature difference from median to consider as background


def fetch_thermal_data():
    """Fetch the latest thermal data from the API server."""
    try:
        response = requests.get(API_URL, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API returned status {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching thermal data: {e}")
        return None


def thermal_data_to_array(data):
    """Convert thermal data to a 2D numpy array."""
    if 't' in data:
        # Compact format: w, h, t (flat array)
        width = data['w']
        height = data['h']
        temps = data['t']
    elif 'pixels' in data:
        # Expanded format: pixels array
        width = data['width']
        height = data['height']
        temps = [p['temp'] for p in data['pixels']]
    else:
        raise ValueError("Unknown thermal data format")
    
    # Reshape flat array to 2D grid
    temp_array = np.array(temps).reshape((height, width))
    return temp_array


def estimate_room_temperature(temp_array):
    """Estimate room/background temperature from thermal data."""
    # Use median temperature as room temperature estimate
    # This filters out hot spots (people) and cold spots
    return np.median(temp_array)


def detect_human_heat(temp_array, room_temp):
    """Create a binary mask of pixels that likely contain human body heat."""
    # Method 1: Absolute temperature threshold
    human_mask = (temp_array >= MIN_HUMAN_TEMP) & (temp_array <= MAX_HUMAN_TEMP)
    
    # Method 2: Relative to room temperature (warmer than room by threshold)
    temp_diff = temp_array - room_temp
    relative_mask = temp_diff >= ROOM_TEMP_THRESHOLD
    
    # Combine both methods (must satisfy both)
    combined_mask = human_mask & relative_mask
    
    return combined_mask.astype(int)


def find_people_clusters(human_mask):
    """Find connected clusters of warm pixels and count them as people."""
    # Use connected components labeling
    # Structure defines connectivity (8-connected: includes diagonals)
    structure = np.ones((3, 3), dtype=int)
    labeled_array, num_features = label(human_mask, structure=structure)
    
    # Filter clusters by size
    people_clusters = []
    for i in range(1, num_features + 1):
        cluster_size = np.sum(labeled_array == i)
        if MIN_CLUSTER_SIZE <= cluster_size <= MAX_CLUSTER_SIZE:
            # Get cluster center and bounds
            cluster_pixels = np.where(labeled_array == i)
            center_row = int(np.mean(cluster_pixels[0]))
            center_col = int(np.mean(cluster_pixels[1]))
            people_clusters.append({
                'id': i,
                'size': cluster_size,
                'center': (center_row, center_col),
                'pixels': list(zip(cluster_pixels[0], cluster_pixels[1]))
            })
    
    return people_clusters, labeled_array


def estimate_occupancy(temp_array):
    """Main function to estimate room occupancy from thermal data."""
    # Convert to 2D array
    temp_array_2d = thermal_data_to_array(temp_array)
    
    # Estimate room temperature
    room_temp = estimate_room_temperature(temp_array_2d)
    
    # Detect human body heat
    human_mask = detect_human_heat(temp_array_2d, room_temp)
    
    # Find people clusters
    people_clusters, labeled_array = find_people_clusters(human_mask)
    
    # Estimate occupancy count
    occupancy_count = len(people_clusters)
    
    # Calculate statistics
    min_temp = float(np.min(temp_array_2d))
    max_temp = float(np.max(temp_array_2d))
    avg_temp = float(np.mean(temp_array_2d))
    
    return {
        'occupancy': occupancy_count,
        'people_clusters': people_clusters,
        'room_temperature': float(room_temp),
        'min_temp': min_temp,
        'max_temp': max_temp,
        'avg_temp': avg_temp,
        'human_mask': human_mask.tolist(),
        'labeled_array': labeled_array.tolist(),
        'timestamp': datetime.now().isoformat()
    }


def print_occupancy_report(result):
    """Print a formatted occupancy estimation report."""
    print("\n" + "=" * 60)
    print("OCCUPANCY ESTIMATION REPORT")
    print("=" * 60)
    print(f"Timestamp: {result['timestamp']}")
    print(f"\nRoom Temperature: {result['room_temperature']:.1f}°C")
    print(f"Temperature Range: {result['min_temp']:.1f}°C - {result['max_temp']:.1f}°C")
    print(f"Average Temperature: {result['avg_temp']:.1f}°C")
    print(f"\n{'='*60}")
    print(f"ESTIMATED OCCUPANCY: {result['occupancy']} person(s)")
    print(f"{'='*60}")
    
    if result['people_clusters']:
        print("\nDetected People:")
        for i, person in enumerate(result['people_clusters'], 1):
            print(f"  Person {i}:")
            print(f"    - Cluster size: {person['size']} pixels")
            print(f"    - Center position: row {person['center'][0]}, col {person['center'][1]}")
    else:
        print("\nNo people detected in thermal image.")
    
    print("\n" + "=" * 60 + "\n")


def run_continuous_monitoring():
    """Continuously monitor occupancy and print reports."""
    print("Starting continuous occupancy monitoring...")
    print(f"Fetching data from: {API_URL}")
    print(f"Update interval: {UPDATE_INTERVAL} seconds")
    print(f"Detection parameters:")
    print(f"  - Human temperature range: {MIN_HUMAN_TEMP}°C - {MAX_HUMAN_TEMP}°C")
    print(f"  - Min cluster size: {MIN_CLUSTER_SIZE} pixels")
    print(f"  - Max cluster size: {MAX_CLUSTER_SIZE} pixels")
    print("\nPress Ctrl+C to stop\n")
    
    try:
        while True:
            # Fetch thermal data
            thermal_data = fetch_thermal_data()
            
            if thermal_data is None:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Failed to fetch thermal data")
                time.sleep(UPDATE_INTERVAL)
                continue
            
            # Estimate occupancy
            try:
                result = estimate_occupancy(thermal_data)
                print_occupancy_report(result)
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Error estimating occupancy: {e}")
            
            # Wait before next update
            time.sleep(UPDATE_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user")


def run_single_analysis():
    """Run a single occupancy analysis."""
    print("Fetching thermal data...")
    thermal_data = fetch_thermal_data()
    
    if thermal_data is None:
        print("Error: Could not fetch thermal data from API server")
        print(f"Make sure the API server is running at {API_URL}")
        return
    
    print("Analyzing thermal image for occupancy...")
    try:
        result = estimate_occupancy(thermal_data)
        print_occupancy_report(result)
        
        # Optionally save result to JSON file
        output_file = f"occupancy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2)
        print(f"Results saved to: {output_file}")
        
    except Exception as e:
        print(f"Error estimating occupancy: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--continuous':
        run_continuous_monitoring()
    else:
        run_single_analysis()
