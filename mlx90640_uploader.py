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
from array import array
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

# Initialize I2C bus at 400 kHz (standard Fast-mode; within MLX90640 spec and more
# reliable over typical wire lengths than 800 kHz).  If the bus is already in use
# (e.g. a previous script didn't deinit it), deinit the singleton and reinitialise
# at the correct frequency — falling back to board.I2C() would give 100 kHz which
# is too slow for the MLX90640 at 4 Hz refresh rate.
gc.collect()
i2c = None
try:
    i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
    gc.collect()
except ValueError as e:
    if "in use" in str(e).lower():
        try:
            if hasattr(board, 'I2C'):
                board.I2C().deinit()
        except Exception:
            pass
        i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
        gc.collect()
    else:
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
        time.sleep(2)  # let lwIP TCP stack finish initialising after association
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
_SEND_EAGAIN_MAX = 200
_SEND_EAGAIN_SLEEP_S = 0.1

# Color mapping moved to server to save ESP32 memory

# Allocated once to avoid repeated heap churn on every upload
_response_buffer = bytearray(512)
# Allocated on first use (after getFrame clears its internal temporaries).
# Worst-case size: 768 pixels × 7 bytes ("-199.9,") = 5376 + ~150 bytes header/footer = 5526.
# 6144 gives a safe margin.
_json_buf = None

INVALID_TEMP_THRESHOLD = -200.0  # Treat anything below this as invalid (e.g. -273.15°C)

# Pre-encoded so generate_thermal_json doesn't allocate a new string/bytes on every call
_SENSOR_ID_BYTES = SENSOR_ID.replace('\\', '\\\\').replace('"', '\\"').encode('utf-8')


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


def sanitize_frame_inplace(frame_data):
    """Replace invalid pixels with the min valid temperature, modifying frame_data in-place.

    Avoids allocating a new 768-element list, which would cause 7-8 list-growth
    reallocations and leave scattered dead blocks that fragment the heap permanently
    (CircuitPython's GC is non-compacting).
    """
    min_valid = None
    for v in frame_data:
        if v is not None and v > INVALID_TEMP_THRESHOLD:
            if min_valid is None or v < min_valid:
                min_valid = v
    if min_valid is None:
        min_valid = 0.0
    for i in range(len(frame_data)):
        if frame_data[i] is None or frame_data[i] <= INVALID_TEMP_THRESHOLD:
            frame_data[i] = min_valid


def generate_thermal_json(frame_data):
    """Write JSON into module-level _json_buf; returns a memoryview of the written bytes.

    Uses slice-assignment instead of bytearray += to avoid repeated reallocs that
    fragment the heap and cause MemoryError on subsequent mlx.getFrame() calls.
    _json_buf is allocated on first call so it doesn't consume startup heap.
    """
    global _json_buf
    if _json_buf is None:
        _json_buf = bytearray(6144)
    min_temp = 999.0
    max_temp = -999.0
    for v in frame_data:
        if v is not None and v > INVALID_TEMP_THRESHOLD:
            if v < min_temp:
                min_temp = v
            if v > max_temp:
                max_temp = v
    if min_temp == 999.0 or max_temp == -999.0:
        min_temp = 0.0
        max_temp = 0.0

    pos = 0

    def wr(b):
        nonlocal pos
        n = len(b)
        _json_buf[pos:pos + n] = b
        pos += n

    wr(b'{"sensor_id":"')
    wr(_SENSOR_ID_BYTES)
    wr(b'","w":')
    wr(str(MLX_SHAPE[1]).encode())
    wr(b',"h":')
    wr(str(MLX_SHAPE[0]).encode())
    wr(b',"min":')
    wr(("%.1f" % min_temp).encode())
    wr(b',"max":')
    wr(("%.1f" % max_temp).encode())
    wr(b',"t":[')
    wr(("%.1f" % frame_data[0]).encode())
    for i in range(1, len(frame_data)):
        _json_buf[pos] = 44  # ','
        pos += 1
        t = ("%.1f" % frame_data[i]).encode()
        n = len(t)
        _json_buf[pos:pos + n] = t
        pos += n
    _json_buf[pos] = 93    # ']'
    _json_buf[pos + 1] = 125  # '}'
    pos += 2
    return memoryview(_json_buf)[:pos]


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
        time.sleep(3)  # let lwIP complete the TCP handshake before sending

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
        time.sleep(2)  # let lwIP TCP stack finish initialising after association
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
last_uploaded_frame = None  # array('f') snapshot after each successful upload; None = upload next
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

        # Sanitize in-place — no new list allocated, no heap fragmentation
        gc.collect()
        try:
            sanitize_frame_inplace(frame)
        except Exception as e:
            print(f"Error sanitizing frame: {e}")
            time.sleep(UPLOAD_INTERVAL)
            continue

        heartbeat_due = (
            last_upload_time is None
            or (time.monotonic() - last_upload_time) >= HEARTBEAT_INTERVAL_S
        )
        if MIN_MEAN_DELTA_C > 0 and last_uploaded_frame is not None and not heartbeat_due:
            delta = mean_abs_frame_diff(frame, last_uploaded_frame)
            if delta < MIN_MEAN_DELTA_C:
                skip_count += 1
                gc.collect()
                time.sleep(UPLOAD_INTERVAL)
                continue

        try:
            json_bytes = generate_thermal_json(frame)
        except Exception as e:
            print(f"Error generating JSON: {e}")
            gc.collect()
            time.sleep(UPLOAD_INTERVAL)
            continue

        min_temp = min(frame)
        max_temp = max(frame)
        if upload_thermal_data(json_bytes):
            upload_count += 1
            consecutive_upload_failures = 0
            # array('f') snapshot: 3 KB instead of ~13 KB for a Python float list
            last_uploaded_frame = array('f', frame)
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
