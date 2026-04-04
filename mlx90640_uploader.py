"""
ESP32 CircuitPython Thermal Camera Data Uploader
Reads thermal data from MLX90640 sensor and uploads to API server
Upload interval is configurable via UPLOAD_INTERVAL variable
"""

import time
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
API_URL = 'http://occupancy-api-container.yellowbush-1452fab1.canadacentral.azurecontainerapps.io/api/thermal'

# Pre-parse API_URL once so upload_thermal_data doesn't repeat this on every upload
_url_no_scheme = API_URL[7:] if API_URL.startswith("http://") else API_URL[8:]
_is_https = API_URL.startswith("https://")
_url_parts = _url_no_scheme.split('/')
_host_port = _url_parts[0].split(':')
API_HOST = _host_port[0]
API_PORT = int(_host_port[1]) if len(_host_port) > 1 and _host_port[1] else (443 if _is_https else 80)
API_PATH = '/' + '/'.join(_url_parts[1:]) if len(_url_parts) > 1 else '/'
del _url_no_scheme, _is_https, _url_parts, _host_port

# Unique sensor ID - set in settings.toml so each device is identifiable (e.g. SENSOR_ID = "living-room")
SENSOR_ID = os.getenv("SENSOR_ID", "default")

# Upload rate - how often to send thermal data to the API (in seconds)
UPLOAD_INTERVAL = 6.0  # Adjust this value to change upload frequency

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
# SU wifi does not need a password
password = os.getenv("WIFI_PASSWORD") 

if not ssid:
    raise ValueError("WiFi credentials not found in settings.toml")

wifi.radio.enabled = True
for attempt in range(5):
    try:
        if password:
            wifi.radio.connect(ssid=ssid, password=password)
        else:
            wifi.radio.connect(ssid=ssid)
        break
    except ConnectionError as e:
        print(f"WiFi attempt {attempt + 1} failed: {e}")
        if attempt < 4:
            time.sleep(2)
else:
    raise ConnectionError("Failed to connect after 5 attempts")

ip_addr = wifi.radio.ipv4_address
gc.collect()

pool = socketpool.SocketPool(wifi.radio)
gc.collect()

# Color mapping moved to server to save ESP32 memory

INVALID_TEMP_THRESHOLD = -200.0  # Treat anything below this as invalid (e.g. -273.15°C)


def sanitize_frame(frame_data):
    """Return a new list where invalid pixels are replaced by the minimum valid temperature.

    This avoids uploading impossible values like -273.15°C while preserving shape.
    """
    # Collect valid temps
    valid = []
    for v in frame_data:
        if v is not None and v > INVALID_TEMP_THRESHOLD:
            valid.append(v)
    if not valid:
        # If everything looks invalid, replace with a safe constant so logs and uploads
        # never show impossible values like -273.15°C.
        return [0.0] * len(frame_data)
    min_valid = min(valid)
    sanitized = []
    for v in frame_data:
        if v is None or v <= INVALID_TEMP_THRESHOLD:
            sanitized.append(min_valid)
        else:
            sanitized.append(v)
    return sanitized


def generate_thermal_json(frame_data):
    """Generate minimal JSON with just raw temperature data - very memory efficient."""
    # Calculate min/max for the server to use, ignoring invalid pixels
    min_temp = 999.0
    max_temp = -999.0
    for v in frame_data:
        if v is not None and v > INVALID_TEMP_THRESHOLD:
            if v < min_temp:
                min_temp = v
            if v > max_temp:
                max_temp = v
    # Fallback if everything was invalid
    if min_temp == 999.0 or max_temp == -999.0:
        min_temp = 0.0
        max_temp = 0.0
    
    # Build JSON into a bytearray: mutable, grows in-place, no per-value string
    # object overhead. The old list+join approach created ~1500 small objects,
    # fragmenting the heap until the final join failed to find a contiguous block.
    safe_id = SENSOR_ID.replace('\\', '\\\\').replace('"', '\\"')
    buf = bytearray(b'{"sensor_id":"')
    buf += safe_id.encode('utf-8')
    buf += b'","w":'
    buf += str(MLX_SHAPE[1]).encode()
    buf += b',"h":'
    buf += str(MLX_SHAPE[0]).encode()
    buf += b',"min":'
    buf += str(round(min_temp, 1)).encode()
    buf += b',"max":'
    buf += str(round(max_temp, 1)).encode()
    buf += b',"t":['
    buf += str(round(frame_data[0], 1)).encode()
    for i in range(1, len(frame_data)):
        buf += b','
        buf += str(round(frame_data[i], 1)).encode()
    buf += b']}'
    return buf

def upload_thermal_data(json_data):
    """Upload thermal data to API server via HTTP POST."""
    try:
        host, port, path = API_HOST, API_PORT, API_PATH

        # Create socket connection
        try:
            socket = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        except (AttributeError, TypeError):
            socket = pool.socket()
        
        try:
            socket.setblocking(True)
            socket.connect((host, port))
            
            # Prepare HTTP POST request
            json_bytes = json_data  # already a bytearray from generate_thermal_json
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

def ensure_wifi_connected():
    """Reconnect to WiFi if the connection has dropped. Returns True if connected."""
    if wifi.radio.connected:
        return True
    print("WiFi disconnected, reconnecting...")
    try:
        if password:
            wifi.radio.connect(ssid=ssid, password=password)
        else:
            wifi.radio.connect(ssid=ssid)
        print(f"Reconnected: {wifi.radio.ipv4_address}")
        return True
    except Exception as e:
        print(f"WiFi reconnect failed: {e}")
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

        if not ensure_wifi_connected():
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
            # Sanitize frame so we don't upload impossible values like -273.15°C
            sanitized_frame = sanitize_frame(frame)
            json_data = generate_thermal_json(sanitized_frame)
        except Exception as e:
            print(f"Error generating JSON: {e}")
            time.sleep(UPLOAD_INTERVAL)
            continue
        
        # Upload to API
        # Use sanitized values for logging so min is realistic
        min_temp = min(sanitized_frame)
        max_temp = max(sanitized_frame)
        del sanitized_frame
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
