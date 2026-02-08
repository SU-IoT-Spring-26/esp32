"""
ESP32 CircuitPython Thermal Camera Data Uploader
Reads thermal data from MLX90640 sensor and uploads to API server
Upload interval is configurable via UPLOAD_INTERVAL variable
"""

import time
import json
import os
import gc
gc.collect()

import board
import busio
import wifi
import socketpool
import adafruit_mlx90640
from adafruit_mlx90640 import RefreshRate

gc.collect()

# Thermal image dimensions
MLX_SHAPE = (24, 32)  # 24 rows, 32 columns
FRAME_SIZE = MLX_SHAPE[0] * MLX_SHAPE[1]  # 768 pixels

# API configuration - modify this to match your laptop's IP address
# Get your laptop's IP with: ip addr show (Linux) or ipconfig (Windows)
API_URL = "http://10.0.0.111:5000/api/thermal"  # Change to your laptop's IP

# Unique sensor ID - set in settings.toml so each device is identifiable (e.g. SENSOR_ID = "living-room")
SENSOR_ID = os.getenv("SENSOR_ID", "default")

# Upload rate - how often to send thermal data to the API (in seconds)
UPLOAD_INTERVAL = 3.0  # Adjust this value to change upload frequency

# Initialize I2C bus
gc.collect()
i2c = None
try:
    i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
    gc.collect()
except ValueError as e:
    if "in use" in str(e).lower() and hasattr(board, 'I2C'):
        i2c = board.I2C()
        gc.collect()
    else:
        raise
except Exception as e:
    raise

# Initialize MLX90640 sensor
gc.collect()
mlx = None
try:
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = RefreshRate.REFRESH_4_HZ
    gc.collect()
except Exception:
    mlx = None

# Frame buffer for thermal data
gc.collect()
frame = [0.0] * FRAME_SIZE
gc.collect()

# WiFi configuration
gc.collect()
ssid = os.getenv("WIFI_SSID")
password = os.getenv("WIFI_PASSWORD")

if not ssid or not password:
    raise ValueError("WiFi credentials not found in settings.toml")

wifi.radio.connect(ssid=ssid, password=password)
ip_addr = wifi.radio.ipv4_address
gc.collect()

pool = socketpool.SocketPool(wifi.radio)
gc.collect()

# Color mapping moved to server to save ESP32 memory

def generate_thermal_json(frame_data):
    """Generate minimal JSON with just raw temperature data - very memory efficient."""
    # Calculate min/max for the server to use
    min_temp = min(frame_data)
    max_temp = max(frame_data)
    
    # Build JSON string directly without creating intermediate lists
    # This is more memory efficient. Include sensor_id for multi-sensor support.
    json_str = '{"sensor_id":"' + SENSOR_ID.replace('\\', '\\\\').replace('"', '\\"') + '"'
    json_str += ',"w":' + str(MLX_SHAPE[1])
    json_str += ',"h":' + str(MLX_SHAPE[0])
    json_str += ',"min":' + str(round(min_temp, 1))
    json_str += ',"max":' + str(round(max_temp, 1))
    json_str += ',"t":[' + str(round(frame_data[0], 1))
    
    # Add remaining temperatures one at a time
    for i in range(1, len(frame_data)):
        json_str += ',' + str(round(frame_data[i], 1))
    
    json_str += ']}'
    return json_str

def upload_thermal_data(json_data):
    """Upload thermal data to API server via HTTP POST."""
    try:
        # Parse URL
        if API_URL.startswith("http://"):
            url_part = API_URL[7:]
        else:
            url_part = API_URL
        
        parts = url_part.split('/')
        host_port = parts[0].split(':')
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 80
        path = '/' + '/'.join(parts[1:]) if len(parts) > 1 else '/'
        
        # Create socket connection
        try:
            socket = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        except (AttributeError, TypeError):
            socket = pool.socket()
        
        try:
            socket.settimeout(10.0)
            socket.connect((host, port))
            
            # Prepare HTTP POST request
            json_bytes = json_data.encode('utf-8')
            request = f"POST {path} HTTP/1.1\r\n"
            request += f"Host: {host}:{port}\r\n"
            request += "Content-Type: application/json\r\n"
            request += f"Content-Length: {len(json_bytes)}\r\n"
            request += "Connection: close\r\n"
            request += "\r\n"
            
            # Send request header
            request_bytes = request.encode('utf-8')
            total_sent = 0
            while total_sent < len(request_bytes):
                sent = socket.send(request_bytes[total_sent:])
                if sent == 0:
                    raise OSError("Connection broken")
                total_sent += sent
            
            # Send JSON data in small chunks
            total_sent = 0
            chunk_size = 256
            while total_sent < len(json_bytes):
                chunk = json_bytes[total_sent:total_sent + chunk_size]
                sent = socket.send(chunk)
                if sent == 0:
                    raise OSError("Connection broken")
                total_sent += sent
            
            # Read response to verify
            response_buffer = bytearray(512)
            try:
                bytes_read = socket.recv_into(response_buffer, 512)
                # Check if response indicates success (200 OK)
                response_str = response_buffer[:bytes_read].decode('utf-8', errors='ignore')
                if '200' in response_str or 'success' in response_str.lower():
                    return True
            except:
                # If we can't read response, assume success if we sent all data
                if total_sent == len(json_bytes):
                    return True
            
            return False
        finally:
            try:
                socket.close()
            except:
                pass
    except OSError as e:
        errno = getattr(e, 'errno', None)
        if errno == 113:  # EHOSTUNREACH
            print(f"Upload error: Host unreachable - check IP address")
        elif errno == 111:  # ECONNREFUSED
            print(f"Upload error: Connection refused - is server running?")
        elif errno == 110:  # ETIMEDOUT
            print(f"Upload error: Connection timeout")
        else:
            print(f"Upload error: {e} (errno: {errno})")
        return False
    except Exception as e:
        print(f"Upload error: {e}")
        return False

# Main loop
print(f"Connected to WiFi: {ip_addr}")
print(f"API server: {API_URL}")
print("Starting thermal data upload...")

if mlx is None:
    print("WARNING: MLX90640 sensor not initialized")
    print("Script will run but no data will be uploaded")

upload_count = 0
while True:
    try:
        if mlx is None:
            print("Sensor not available, waiting...")
            time.sleep(UPLOAD_INTERVAL)
            continue
        
        # Read thermal frame
        gc.collect()
        try:
            mlx.getFrame(frame)
        except MemoryError:
            print("Memory error reading frame, retrying...")
            gc.collect()
            time.sleep(UPLOAD_INTERVAL)
            continue
        except Exception as e:
            print(f"Error reading frame: {e}")
            time.sleep(UPLOAD_INTERVAL)
            continue
        
        # Generate JSON
        gc.collect()
        try:
            json_data = generate_thermal_json(frame)
        except Exception as e:
            print(f"Error generating JSON: {e}")
            time.sleep(UPLOAD_INTERVAL)
            continue
        
        # Upload to API
        min_temp = min(frame)
        max_temp = max(frame)
        if upload_thermal_data(json_data):
            upload_count += 1
            print(f"Upload #{upload_count}: {min_temp:.1f}°C - {max_temp:.1f}°C")
        else:
            print(f"Upload failed: {min_temp:.1f}°C - {max_temp:.1f}°C")
        
        # Clean up
        del json_data
        gc.collect()
        
        # Wait before next upload
        time.sleep(UPLOAD_INTERVAL)
        
    except KeyboardInterrupt:
        print("\nStopped by user")
        break
    except Exception as e:
        print(f"Error in main loop: {e}")
        time.sleep(UPLOAD_INTERVAL)
