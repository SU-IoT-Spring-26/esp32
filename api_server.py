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

app = Flask(__name__)
CORS(app)  # Enable CORS for ESP32 requests

# Store the latest thermal data
latest_thermal_data = None
last_update_time = None

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

@app.route('/api/thermal', methods=['POST'])
def receive_thermal_data():
    """Receive thermal data from ESP32."""
    global latest_thermal_data, last_update_time
    
    try:
        data = request.get_json()
        if not data:
            print("ERROR: No JSON data received")
            return jsonify({"error": "No data received"}), 400
        
        print(f"Received data: keys={list(data.keys())}, has 't'={('t' in data)}")
        
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
        
        last_update_time = datetime.now().isoformat()
        
        pixel_count = len(latest_thermal_data.get('pixels', []))
        print(f"Success: stored {pixel_count} pixels")
        return jsonify({"status": "success", "received": pixel_count}), 200
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
        <div class="status" id="status">Waiting for data...</div>
    </div>
    <canvas id="thermalCanvas" width="320" height="240"></canvas>
    
    <script>
        const canvas = document.getElementById('thermalCanvas');
        const ctx = canvas.getContext('2d');
        
        function refreshImage() {
            fetch('/api/thermal')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        document.getElementById('status').textContent = 'No data available';
                        return;
                    }
                    drawThermalImage(data);
                    document.getElementById('minTemp').textContent = data.min_temp;
                    document.getElementById('maxTemp').textContent = data.max_temp;
                    if (data.last_update) {
                        const updateTime = new Date(data.last_update).toLocaleTimeString();
                        document.getElementById('status').textContent = 'Last update: ' + updateTime;
                    }
                })
                .catch(error => {
                    document.getElementById('status').textContent = 'Error: ' + error;
                    console.error('Error:', error);
                });
        }
        
        function drawThermalImage(data) {
            const pixelSize = Math.min(
                Math.floor(canvas.width / data.width),
                Math.floor(canvas.height / data.height)
            );
            
            const offsetX = (canvas.width - data.width * pixelSize) / 2;
            const offsetY = (canvas.height - data.height * pixelSize) / 2;
            
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            
            data.pixels.forEach(pixel => {
                ctx.fillStyle = `rgb(${pixel.r}, ${pixel.g}, ${pixel.b})`;
                ctx.fillRect(
                    offsetX + pixel.col * pixelSize,
                    offsetY + pixel.row * pixelSize,
                    pixelSize,
                    pixelSize
                );
            });
        }
        
        // Refresh every 1 second
        refreshImage();
        setInterval(refreshImage, 1000);
    </script>
</body>
</html>"""

@app.route('/api/test', methods=['GET'])
def test_endpoint():
    """Test endpoint to verify server is running."""
    return jsonify({"status": "server is running", "time": datetime.now().isoformat()}), 200

if __name__ == '__main__':
    print("=" * 60)
    print("Thermal Camera API Server")
    print("=" * 60)
    print("API endpoint: http://0.0.0.0:5000/api/thermal")
    print("Test endpoint: http://0.0.0.0:5000/api/test")
    print("Web interface: http://localhost:5000")
    print("=" * 60)
    print("\nWaiting for ESP32 to send thermal data...\n")
    
    # Run on all interfaces so ESP32 can connect
    app.run(host='0.0.0.0', port=5000, debug=True)
