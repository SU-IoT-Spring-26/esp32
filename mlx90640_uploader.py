"""
ESP32 CircuitPython Thermal Camera Data Uploader
Reads thermal data from MLX90640 sensor and uploads to API server.

Poll interval: UPLOAD_INTERVAL. When MIN_MEAN_DELTA_C > 0, uploads only if the
sanitized frame differs from the last successful upload by at least that mean
absolute °C across pixels. Set MIN_MEAN_DELTA_C to 0 to upload every poll.
A heartbeat upload is forced at least every HEARTBEAT_INTERVAL_S seconds
regardless of the delta gate.
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

# Pre-parse API_URL once so upload_thermal_data doesn't repeat this on every upload.
# Plain HTTP only (this uploader does not use TLS on the device).
if not API_URL.startswith("http://"):
    raise ValueError("API_URL must start with http://")
_url_no_scheme = API_URL[7:]
_url_parts = _url_no_scheme.split('/')
_host_port = _url_parts[0].split(':')
API_HOST = _host_port[0]
API_PORT = int(_host_port[1]) if len(_host_port) > 1 and _host_port[1] else 80
API_PATH = '/' + '/'.join(_url_parts[1:]) if len(_url_parts) > 1 else '/'
del _url_no_scheme, _url_parts, _host_port

# Cached IPv4 for connect(); HTTP Host: still uses API_HOST (hostname).
API_RESOLVED_IP = None

# Unique sensor ID - set in settings.toml so each device is identifiable (e.g. SENSOR_ID = "living-room")
SENSOR_ID = os.getenv("SENSOR_ID", "default")

# How often to poll the sensor (seconds). Upload runs when the frame changed
# enough vs last successful upload (see MIN_MEAN_DELTA_C), if enabled.
UPLOAD_INTERVAL = 15.0

# Only upload when mean |current − last_uploaded| across all pixels exceeds this (°C).
# Reduces API traffic when the scene is static. Set to 0 to upload every poll.
MIN_MEAN_DELTA_C = 0.2

# Force an upload at least this often (seconds), even if the scene hasn't changed.
HEARTBEAT_INTERVAL_S = 600.0

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
    except (ConnectionError, OSError, RuntimeError) as e:
        print(f"WiFi attempt {attempt + 1} failed: {e}")
        if attempt < 4:
            time.sleep(2)
else:
    raise ConnectionError("Failed to connect after 5 attempts")

ip_addr = wifi.radio.ipv4_address
gc.collect()

pool = socketpool.SocketPool(wifi.radio)
gc.collect()

# Upload robustness (EAGAIN on send, flaky DNS)
UPLOAD_MAX_ATTEMPTS = 3
UPLOAD_RETRY_DELAY_S = 1.0
SOCKET_TIMEOUT_S = 20.0
_SEND_EAGAIN_MAX = 50
_SEND_EAGAIN_SLEEP_S = 0.05

# Color mapping moved to server to save ESP32 memory

# Allocated once to avoid repeated heap churn on every upload
_response_buffer = bytearray(512)

INVALID_TEMP_THRESHOLD = -200.0  # Treat anything below this as invalid (e.g. -273.15°C)


def mean_abs_frame_diff(a, b):
    """Mean absolute difference between two equal-length frames (°C)."""
    total = 0.0
    for i in range(FRAME_SIZE):
        d = a[i] - b[i]
        if d < 0:
            d = -d
        total += d
    return total / FRAME_SIZE


def _prefetch_api_ip():
    """Resolve API_HOST once; connect uses IP to reduce per-request DNS failures."""
    global API_RESOLVED_IP
    if API_RESOLVED_IP is not None:
        return
    try:
        try:
            infos = pool.getaddrinfo(API_HOST, API_PORT, 0, pool.SOCK_STREAM)
        except TypeError:
            infos = pool.getaddrinfo(API_HOST, API_PORT)
    except Exception as e:
        print(f"DNS prefetch skipped: {e}")
        return
    _af = getattr(pool, "AF_INET", 2)
    for info in infos:
        if info[0] == _af:
            API_RESOLVED_IP = info[-1][0]
            print(f"API {API_HOST} -> {API_RESOLVED_IP}")
            return
    if infos:
        API_RESOLVED_IP = infos[0][-1][0]
        print(f"API {API_HOST} -> {API_RESOLVED_IP}")


def _send_all_eagain(sock, data):
    """Send buffer in chunks; retry on EAGAIN (errno 11)."""
    total = 0
    n = len(data)
    chunk_size = 256
    while total < n:
        end = min(total + chunk_size, n)
        chunk = data[total:end]
        offset = 0
        clen = len(chunk)
        while offset < clen:
            tries = 0
            while True:
                try:
                    sent = sock.send(chunk[offset:])
                    break
                except OSError as ex:
                    err = getattr(ex, "errno", None)
                    if err == 11:
                        tries += 1
                        if tries > _SEND_EAGAIN_MAX:
                            raise
                        time.sleep(_SEND_EAGAIN_SLEEP_S)
                    else:
                        raise
            if sent == 0:
                raise OSError("Connection broken")
            offset += sent
        total += clen


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


def _upload_thermal_data_once(json_data):
    """Single HTTP POST attempt. Uses cached IP for TCP if available."""
    port, path = API_PORT, API_PATH
    peer = API_RESOLVED_IP if API_RESOLVED_IP is not None else API_HOST
    sock = None
    try:
        try:
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        except (AttributeError, TypeError):
            sock = pool.socket()
        sock.settimeout(SOCKET_TIMEOUT_S)
        sock.connect((peer, port))

        json_bytes = json_data
        host_header = API_HOST if port == 80 else f"{API_HOST}:{port}"
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(json_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        _send_all_eagain(sock, request.encode("utf-8"))
        _send_all_eagain(sock, json_bytes)

        try:
            bytes_read = sock.recv_into(_response_buffer, 512)
            if bytes_read == 0:
                return True  # body sent; peer closed before headers (unusual but ok)
            response_str = _response_buffer[:bytes_read].decode("utf-8", errors="ignore")
            if "200" in response_str or "success" in response_str.lower():
                return True
        except Exception:
            return True  # optimistic if we fully sent the request body
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _print_upload_oserror(e):
    errno = getattr(e, "errno", None)
    if errno == 113:
        print("Upload error: Host unreachable - check IP address")
    elif errno == 111:
        print("Upload error: Connection refused - is server running?")
    elif errno in (110, 116):
        print("Upload error: Connection timeout")
    elif errno in (-2, 2) or (errno is None and "Name or service" in str(e)):
        print("Upload error: DNS failed (will retry)")
        global API_RESOLVED_IP
        API_RESOLVED_IP = None
    elif errno == 11:
        print("Upload error: EAGAIN (will retry)")
    else:
        print(f"Upload error: {e} (errno: {errno})")


def upload_thermal_data(json_data):
    """Upload thermal data to API server via HTTP POST; retries transient failures."""
    if API_RESOLVED_IP is None:
        _prefetch_api_ip()
    for attempt in range(UPLOAD_MAX_ATTEMPTS):
        try:
            if _upload_thermal_data_once(json_data):
                return True
        except OSError as e:
            _print_upload_oserror(e)
        except Exception as e:
            print(f"Upload error: {e}")
        if attempt + 1 < UPLOAD_MAX_ATTEMPTS:
            time.sleep(UPLOAD_RETRY_DELAY_S)
    if UPLOAD_MAX_ATTEMPTS > 1:
        print(f"Upload gave up after {UPLOAD_MAX_ATTEMPTS} attempts")
    return False


def ensure_wifi_connected():
    """Reconnect to WiFi if the connection has dropped. Returns True if connected."""
    global pool, API_RESOLVED_IP
    if wifi.radio.connected:
        return True
    print("WiFi disconnected, reconnecting...")
    try:
        if password:
            wifi.radio.connect(ssid=ssid, password=password)
        else:
            wifi.radio.connect(ssid=ssid)
        API_RESOLVED_IP = None
        pool = socketpool.SocketPool(wifi.radio)
        _prefetch_api_ip()
        print(f"Reconnected: {wifi.radio.ipv4_address}")
        return True
    except Exception as e:
        print(f"WiFi reconnect failed: {e}")
        return False


# Main loop
print(f"Connected to WiFi: {ip_addr}")
print(f"API server: {API_URL}")
_prefetch_api_ip()
print("Starting thermal data upload...")

if mlx is None:
    print("WARNING: MLX90640 sensor not initialized")
    print("Script will run but no data will be uploaded")

upload_count = 0
skip_count = 0
last_uploaded_frame = None  # sanitized frame from last successful upload; None = upload next
last_upload_time = None     # monotonic timestamp of last successful upload
sensor_fail_count = 0
MAX_SENSOR_FAILS = 5  # Re-initialize sensor after this many consecutive failures
consecutive_upload_failures = 0
MAX_UPLOAD_FAILURES_BEFORE_RECONNECT = 5  # ~75 s of failed uploads triggers a WiFi radio cycle

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
            sensor_fail_count = 0
        except MemoryError:
            print("Memory error reading frame, retrying...")
            gc.collect()
            time.sleep(UPLOAD_INTERVAL)
            continue
        except Exception as e:
            sensor_fail_count += 1
            print(f"Error reading frame ({sensor_fail_count}/{MAX_SENSOR_FAILS}): {e}")
            if sensor_fail_count >= MAX_SENSOR_FAILS:
                print("Too many sensor failures, re-initializing MLX90640...")
                sensor_fail_count = 0
                try:
                    gc.collect()
                    mlx = adafruit_mlx90640.MLX90640(i2c)
                    mlx.refresh_rate = RefreshRate.REFRESH_4_HZ
                    gc.collect()
                    print("Sensor re-initialized successfully")
                except Exception as reinit_e:
                    print(f"Sensor re-init failed: {reinit_e}")
                    mlx = None
            time.sleep(UPLOAD_INTERVAL)
            continue

        # Sanitize and optionally skip if scene barely changed vs last upload
        gc.collect()
        try:
            sanitized_frame = sanitize_frame(frame)
        except Exception as e:
            print(f"Error sanitizing frame: {e}")
            time.sleep(UPLOAD_INTERVAL)
            continue

        heartbeat_due = (
            last_upload_time is None
            or (time.monotonic() - last_upload_time) >= HEARTBEAT_INTERVAL_S
        )
        if MIN_MEAN_DELTA_C > 0 and last_uploaded_frame is not None and not heartbeat_due:
            delta = mean_abs_frame_diff(sanitized_frame, last_uploaded_frame)
            if delta < MIN_MEAN_DELTA_C:
                skip_count += 1
                del sanitized_frame
                gc.collect()
                time.sleep(UPLOAD_INTERVAL)
                continue

        try:
            json_data = generate_thermal_json(sanitized_frame)
        except Exception as e:
            print(f"Error generating JSON: {e}")
            del sanitized_frame
            gc.collect()
            time.sleep(UPLOAD_INTERVAL)
            continue

        # Upload to API (use sanitized values for logging so min is realistic)
        min_temp = min(sanitized_frame)
        max_temp = max(sanitized_frame)
        if upload_thermal_data(json_data):
            upload_count += 1
            consecutive_upload_failures = 0
            last_uploaded_frame = sanitized_frame
            last_upload_time = time.monotonic()
            msg = f"Upload #{upload_count}: {min_temp:.1f}°C - {max_temp:.1f}°C"
            if skip_count:
                msg += f" (skipped {skip_count} unchanged)"
                skip_count = 0
            print(msg)
        else:
            consecutive_upload_failures += 1
            print(f"Upload failed ({consecutive_upload_failures}/{MAX_UPLOAD_FAILURES_BEFORE_RECONNECT}): {min_temp:.1f}°C - {max_temp:.1f}°C")
            if consecutive_upload_failures >= MAX_UPLOAD_FAILURES_BEFORE_RECONNECT:
                # wifi.radio.connected can stay True even when the connection is broken (associated
                # but not routing). Cycle the radio so ensure_wifi_connected() does a clean reconnect.
                print("Cycling WiFi radio after repeated upload failures...")
                consecutive_upload_failures = 0
                try:
                    wifi.radio.enabled = False
                    gc.collect()
                    time.sleep(2)
                    wifi.radio.enabled = True
                except Exception as e:
                    print(f"WiFi radio cycle error: {e}")

        del sanitized_frame
        del json_data
        gc.collect()

        # Wait before next upload
        time.sleep(UPLOAD_INTERVAL)

    except KeyboardInterrupt:
        # Ignore DTR toggles from serial terminal connect/disconnect on CH340/CP2102 boards.
        # A real Ctrl+C from Thonny will send repeated interrupts and can still stop via the REPL.
        pass
    except Exception as e:
        print(f"Error in main loop: {e}")
        time.sleep(UPLOAD_INTERVAL)
