#!/usr/bin/env python3
"""
Thermal Camera API Server
Runs on your laptop to receive thermal data from ESP32
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
from datetime import datetime
import os
from pathlib import Path
from collections import Counter
import numpy as np
from scipy.ndimage import label

app = Flask(__name__)
CORS(app)  # Enable CORS for ESP32 requests

# Configuration
DATA_DIR = Path("thermal_data")  # Directory to save thermal data files
SAVE_DATA = True  # Set to False to disable saving to disk

# Occupancy detection parameters
MIN_HUMAN_TEMP = 30.0  # Minimum temperature to consider as human (°C)
MAX_HUMAN_TEMP = 45.0  # Maximum temperature to consider as human (°C)
MIN_CLUSTER_SIZE = 3   # Minimum number of connected pixels to count as a person
MAX_CLUSTER_SIZE = 200 # Maximum cluster size (to filter out large hot objects)
ROOM_TEMP_THRESHOLD = 0.5  # Temperature difference from median to consider as background

# Store the latest thermal data
latest_thermal_data = None
last_update_time = None
latest_occupancy = None  # Store latest occupancy estimate
_data_counter = 0  # Counter for sequential file naming

# Create data directory if it doesn't exist
if SAVE_DATA:
    DATA_DIR.mkdir(exist_ok=True)
    print(f"Thermal data will be saved to: {DATA_DIR.absolute()}")

def temperature_to_color(temp, min_temp, max_temp):
    """Convert temperature to RGB color - server-side color mapping."""
    temp = max(min_temp, min(max_temp, temp))
    
    if max_temp == min_temp:
        return (128, 128, 128)
    
    normalized = (temp - min_temp) / (max_temp - min_temp)
    
    if normalized < 0.25:
        r, g, b = 0, int(normalized * 4 * 255), 255
    elif normalized < 0.5:
        r, g, b = 0, 255, int((1 - (normalized - 0.25) * 4) * 255)
    elif normalized < 0.75:
        r, g, b = int((normalized - 0.5) * 4 * 255), 255, 0
    else:
        r, g, b = 255, int((1 - (normalized - 0.75) * 4) * 255), 0
    
    return (r, g, b)

def expand_thermal_data(compact_data):
    """Expand compact temperature data into full pixel array with colors."""
    width = compact_data['w']
    height = compact_data['h']
    min_temp = compact_data['min']
    max_temp = compact_data['max']
    temps = compact_data['t']
    
    pixels = []
    for i, temp in enumerate(temps):
        row = i // width
        col = i % width
        r, g, b = temperature_to_color(temp, min_temp, max_temp)
        pixels.append({
            "row": row,
            "col": col,
            "temp": temp,
            "r": r,
            "g": g,
            "b": b
        })
    
    return {
        "width": width,
        "height": height,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "pixels": pixels
    }

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
                'center': (center_row, center_col)
            })
    
    return people_clusters

def estimate_occupancy(thermal_data):
    """Estimate room occupancy from thermal data."""
    try:
        # Convert to 2D array
        temp_array_2d = thermal_data_to_array(thermal_data)
        
        # Estimate room temperature
        room_temp = estimate_room_temperature(temp_array_2d)
        
        # Detect human body heat
        human_mask = detect_human_heat(temp_array_2d, room_temp)
        
        # Find people clusters
        people_clusters = find_people_clusters(human_mask)
        
        # Estimate occupancy count
        occupancy_count = len(people_clusters)
        
        return {
            'occupancy': occupancy_count,
            'room_temperature': float(room_temp),
            'people_clusters': people_clusters
        }
    except Exception as e:
        print(f"Error estimating occupancy: {e}")
        return {
            'occupancy': 0,
            'room_temperature': None,
            'people_clusters': [],
            'error': str(e)
        }

def save_thermal_data(compact_data, expanded_data):
    """Save thermal data to disk."""
    global _data_counter
    
    if not SAVE_DATA:
        return
    
    try:
        timestamp = datetime.now()
        timestamp_str = timestamp.strftime('%Y%m%d_%H%M%S_%f')[:-3]  # Include milliseconds
        
        # Save compact format (original, smaller file)
        compact_filename = DATA_DIR / f"thermal_{timestamp_str}_compact.json"
        with open(compact_filename, 'w') as f:
            json.dump({
                "timestamp": timestamp.isoformat(),
                "format": "compact",
                "data": compact_data
            }, f, indent=2)
        
        # Save expanded format (with color data, for analysis)
        expanded_filename = DATA_DIR / f"thermal_{timestamp_str}_expanded.json"
        with open(expanded_filename, 'w') as f:
            json.dump({
                "timestamp": timestamp.isoformat(),
                "format": "expanded",
                "data": expanded_data
            }, f, indent=2)
        
        _data_counter += 1
        print(f"Saved thermal data: {compact_filename.name} ({expanded_filename.name})")
        
    except Exception as e:
        print(f"Error saving thermal data to disk: {e}")

def convert_numpy_types(obj):
    """Recursively convert numpy types to native Python types for JSON serialization."""
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_numpy_types(item) for item in obj)
    else:
        return obj

def save_occupancy_data(occupancy_result):
    """Save occupancy estimation to disk with timestamp."""
    if not SAVE_DATA:
        return
    
    try:
        timestamp = datetime.now()
        
        # Create occupancy log file (one per day)
        date_str = timestamp.strftime('%Y%m%d')
        occupancy_log_file = DATA_DIR / f"occupancy_{date_str}.jsonl"
        
        # Convert numpy types to native Python types for JSON serialization
        occupancy_entry = {
            "timestamp": timestamp.isoformat(),
            "occupancy": int(occupancy_result['occupancy']),
            "room_temperature": float(occupancy_result.get('room_temperature')) if occupancy_result.get('room_temperature') is not None else None,
            "people_clusters": convert_numpy_types(occupancy_result.get('people_clusters', []))
        }
        
        with open(occupancy_log_file, 'a') as f:
            f.write(json.dumps(occupancy_entry) + '\n')
        
    except Exception as e:
        print(f"Error saving occupancy data to disk: {e}")
        import traceback
        traceback.print_exc()

@app.route('/api/thermal', methods=['POST'])
def receive_thermal_data():
    """Receive thermal data from ESP32."""
    global latest_thermal_data, last_update_time, latest_occupancy
    
    try:
        # Handle potential client disconnection gracefully
        try:
            data = request.get_json()
        except Exception as e:
            # Client disconnected or invalid request
            print(f"Error reading request data: {e}")
            return jsonify({"error": "Invalid request"}), 400
        
        if not data:
            print("ERROR: No JSON data received")
            return jsonify({"error": "No data received"}), 400
        
        print(f"Received data: keys={list(data.keys())}, has 't'={('t' in data)}")
        
        # Store original compact data for saving
        compact_data = data.copy()
        
        # Expand compact data format into full format for web display
        if 't' in data:  # Compact format from ESP32
            try:
                expanded_data = expand_thermal_data(data)
                latest_thermal_data = expanded_data
                print(f"Expanded to {len(expanded_data.get('pixels', []))} pixels")
            except Exception as e:
                print(f"Error expanding data: {e}")
                return jsonify({"error": f"Data expansion failed: {e}"}), 500
        else:  # Full format (backwards compatible)
            latest_thermal_data = data
            expanded_data = data
        
        # Estimate occupancy
        occupancy_result = estimate_occupancy(data)
        latest_occupancy = occupancy_result
        print(f"Occupancy estimate: {occupancy_result['occupancy']} person(s)")
        
        last_update_time = datetime.now().isoformat()
        
        # Save to disk
        save_thermal_data(compact_data, expanded_data)
        save_occupancy_data(occupancy_result)
        
        pixel_count = len(latest_thermal_data.get('pixels', []))
        print(f"Success: stored {pixel_count} pixels")
        return jsonify({"status": "success", "received": pixel_count, "occupancy": occupancy_result['occupancy']}), 200
    except Exception as e:
        print(f"ERROR in receive_thermal_data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/thermal', methods=['GET'])
def get_thermal_data():
    """Get the latest thermal data."""
    if latest_thermal_data is None:
        return jsonify({"error": "No data available"}), 404
    
    response = latest_thermal_data.copy()
    response['last_update'] = last_update_time
    if latest_occupancy is not None:
        response['occupancy'] = latest_occupancy['occupancy']
        response['room_temperature'] = latest_occupancy.get('room_temperature')
    return jsonify(response), 200

@app.route('/')
def index():
    """Serve the web interface."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>ESP32 Thermal Camera</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #1a1a1a;
            color: #fff;
            margin: 0;
            padding: 20px;
            text-align: center;
        }
        h1 {
            margin-bottom: 10px;
        }
        .info {
            margin: 10px 0;
            font-size: 14px;
        }
        #thermalCanvas {
            border: 2px solid #333;
            background: #000;
            margin: 20px auto;
            display: block;
            image-rendering: pixelated;
            image-rendering: crisp-edges;
        }
        .status {
            margin: 10px 0;
            font-size: 12px;
            color: #aaa;
        }
    </style>
</head>
<body>
    <h1>ESP32 Thermal Camera</h1>
    <div class="info">
        <div>Min: <span id="minTemp">--</span>°C | Max: <span id="maxTemp">--</span>°C</div>
        <div style="font-size: 18px; margin: 15px 0; font-weight: bold;">
            Occupancy: <span id="occupancy">--</span> person(s)
        </div>
        <div style="font-size: 14px; color: #aaa; margin-bottom: 10px;">
            Room Temp: <span id="roomTemp">--</span>°C
        </div>
        <div class="status" id="status">Waiting for data...</div>
    </div>
    <canvas id="thermalCanvas" width="320" height="240"></canvas>
    
    <div style="margin: 30px auto; max-width: 800px;">
        <h2 style="font-size: 18px; margin-bottom: 10px;">Occupancy Over Time (Today)</h2>
        <canvas id="occupancyChart" style="background: #2a2a2a; border: 1px solid #444;"></canvas>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script>
        const thermalCanvas = document.getElementById('thermalCanvas');
        const thermalCtx = thermalCanvas.getContext('2d');
        
        function refreshImage() {
            fetch('/api/thermal')
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('Received data:', data); // Debug log
                    if (data.error) {
                        document.getElementById('status').textContent = 'No data available: ' + data.error;
                        document.getElementById('occupancy').textContent = '--';
                        document.getElementById('roomTemp').textContent = '--';
                        return;
                    }
                    
                    // Check if we have pixel data
                    if (!data.pixels || !Array.isArray(data.pixels) || data.pixels.length === 0) {
                        console.error('No pixel data in response:', data);
                        document.getElementById('status').textContent = 'No pixel data available';
                        return;
                    }
                    
                    drawThermalImage(data);
                    document.getElementById('minTemp').textContent = data.min_temp || '--';
                    document.getElementById('maxTemp').textContent = data.max_temp || '--';
                    
                    // Update occupancy
                    if (data.occupancy !== undefined) {
                        document.getElementById('occupancy').textContent = data.occupancy;
                    } else {
                        document.getElementById('occupancy').textContent = '--';
                    }
                    
                    // Update room temperature
                    if (data.room_temperature !== undefined && data.room_temperature !== null) {
                        document.getElementById('roomTemp').textContent = data.room_temperature.toFixed(1);
                    } else {
                        document.getElementById('roomTemp').textContent = '--';
                    }
                    
                    if (data.last_update) {
                        const updateTime = new Date(data.last_update).toLocaleTimeString();
                        document.getElementById('status').textContent = 'Last update: ' + updateTime;
                    } else {
                        document.getElementById('status').textContent = 'Data received';
                    }
                })
                .catch(error => {
                    console.error('Error fetching thermal data:', error);
                    document.getElementById('status').textContent = 'Error: ' + error.message;
                    document.getElementById('occupancy').textContent = '--';
                    document.getElementById('roomTemp').textContent = '--';
                });
        }
        
        function drawThermalImage(data) {
            const pixelSize = Math.min(
                Math.floor(thermalCanvas.width / data.width),
                Math.floor(thermalCanvas.height / data.height)
            );
            
            const offsetX = (thermalCanvas.width - data.width * pixelSize) / 2;
            const offsetY = (thermalCanvas.height - data.height * pixelSize) / 2;
            
            thermalCtx.clearRect(0, 0, thermalCanvas.width, thermalCanvas.height);
            
            data.pixels.forEach(pixel => {
                thermalCtx.fillStyle = `rgb(${pixel.r}, ${pixel.g}, ${pixel.b})`;
                thermalCtx.fillRect(
                    offsetX + pixel.col * pixelSize,
                    offsetY + pixel.row * pixelSize,
                    pixelSize,
                    pixelSize
                );
            });
        }
        
        // Initialize occupancy chart
        const chartCtx = document.getElementById('occupancyChart').getContext('2d');
        const occupancyChart = new Chart(chartCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: 'Occupancy',
                    data: [],
                    borderColor: 'rgb(75, 192, 192)',
                    backgroundColor: 'rgba(75, 192, 192, 0.2)',
                    tension: 0.1,
                    fill: true
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                plugins: {
                    legend: {
                        labels: {
                            color: '#fff'
                        }
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#aaa',
                            maxTicksLimit: 12
                        },
                        grid: {
                            color: '#333'
                        }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            color: '#aaa',
                            stepSize: 1
                        },
                        grid: {
                            color: '#333'
                        }
                    }
                }
            }
        });
        
        // Function to update occupancy chart
        function updateOccupancyChart() {
            fetch('/api/occupancy/history')
                .then(response => response.json())
                .then(data => {
                    if (data.error || !data.data || data.data.length === 0) {
                        return;
                    }
                    
                    // Process data for chart
                    const labels = [];
                    const occupancyValues = [];
                    
                    data.data.forEach(entry => {
                        const date = new Date(entry.timestamp);
                        const timeStr = date.toLocaleTimeString('en-US', { 
                            hour: '2-digit', 
                            minute: '2-digit',
                            hour12: false 
                        });
                        labels.push(timeStr);
                        occupancyValues.push(entry.occupancy);
                    });
                    
                    // Update chart
                    occupancyChart.data.labels = labels;
                    occupancyChart.data.datasets[0].data = occupancyValues;
                    occupancyChart.update('none'); // 'none' mode for smooth updates
                })
                .catch(error => {
                    console.error('Error fetching occupancy history:', error);
                });
        }
        
        // Refresh thermal image every 1 second
        refreshImage();
        setInterval(refreshImage, 1000);
        
        // Update occupancy chart every 5 seconds
        updateOccupancyChart();
        setInterval(updateOccupancyChart, 5000);
    </script>
</body>
</html>"""

@app.route('/api/test', methods=['GET'])
def test_endpoint():
    """Test endpoint to verify server is running."""
    return jsonify({"status": "server is running", "time": datetime.now().isoformat()}), 200

@app.route('/api/occupancy/history', methods=['GET'])
def get_occupancy_history():
    """Get historical occupancy data."""
    try:
        # Get date parameter (default to today)
        date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
        occupancy_log_file = DATA_DIR / f"occupancy_{date_str}.jsonl"
        
        if not occupancy_log_file.exists():
            return jsonify({"error": f"No occupancy data found for date {date_str}"}), 404
        
        # Read all lines from the log file
        occupancy_data = []
        with open(occupancy_log_file, 'r') as f:
            for line in f:
                if line.strip():
                    occupancy_data.append(json.loads(line))
        
        return jsonify({
            "date": date_str,
            "count": len(occupancy_data),
            "data": occupancy_data
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/occupancy/stats', methods=['GET'])
def get_occupancy_stats():
    """Get occupancy statistics for a date."""
    try:
        # Get date parameter (default to today)
        date_str = request.args.get('date', datetime.now().strftime('%Y%m%d'))
        occupancy_log_file = DATA_DIR / f"occupancy_{date_str}.jsonl"
        
        if not occupancy_log_file.exists():
            return jsonify({"error": f"No occupancy data found for date {date_str}"}), 404
        
        # Read all lines and calculate statistics
        occupancy_values = []
        with open(occupancy_log_file, 'r') as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    occupancy_values.append(entry['occupancy'])
        
        if not occupancy_values:
            return jsonify({"error": "No occupancy data available"}), 404
        
        # Calculate statistics
        stats = {
            "date": date_str,
            "total_readings": len(occupancy_values),
            "min_occupancy": min(occupancy_values),
            "max_occupancy": max(occupancy_values),
            "avg_occupancy": round(sum(occupancy_values) / len(occupancy_values), 2),
            "current_occupancy": occupancy_values[-1] if occupancy_values else 0
        }
        
        # Count occurrences of each occupancy level
        occupancy_counts = Counter(occupancy_values)
        stats["occupancy_distribution"] = dict(occupancy_counts)
        
        return jsonify(stats), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("Thermal Camera API Server")
    print("=" * 60)
    print("API endpoint: http://0.0.0.0:5000/api/thermal")
    print("Test endpoint: http://0.0.0.0:5000/api/test")
    print("Web interface: http://localhost:5000")
    if SAVE_DATA:
        print(f"Data storage: ENABLED ({DATA_DIR.absolute()})")
    else:
        print("Data storage: DISABLED")
    print("=" * 60)
    print("\nWaiting for ESP32 to send thermal data...\n")
    
    # Run on all interfaces so ESP32 can connect
    app.run(host='0.0.0.0', port=5000, debug=True)
