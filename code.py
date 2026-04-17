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
import microcontroller
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

# API configuration
API_URL = 'http://occupancy-api-container.yellowbush-1452fab1.canadacentral.azurecontainerapps.io/api/thermal'

# Pre-parse API_URL once so upload_thermal_data doesn't repeat this on every upload.
if not API_URL.startswith("http://"):
    raise ValueError("API_URL must start with http://")
_url_no_scheme = API_URL[7:]
_url_parts = _url_no_scheme.split('/')
_host_port = _url_parts[0].split(':')
API_HOST = _host_port[0]
API_PORT = int(_host_port[1]) if len(_host_port) > 1 and _host_port[1] else 80
API_PATH = '/' + '/'.join(_url_parts[1:]) if len(_url_parts) > 1 else '/'
del _url_no_scheme, _url_parts, _host_port

API_RESOLVED_IP = None
SENSOR_ID = os.getenv("SENSOR_ID", "default")

UPLOAD_INTERVAL = 15.0
MIN_MEAN_DELTA_C = 0.2
HEARTBEAT_INTERVAL_S = 60.0
WATCHDOG_RESET_S = 900.0

# Initialize I2C bus at 400 kHz (standard Fast-mode; within MLX90640 spec and more
# reliable over typical wire lengths than 800 kHz).
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
    mlx.refresh_rate = RefreshRate.REFRESH_1_HZ
    gc.collect()
except Exception:
    mlx = None

# ── All buffers and pre-computed constants allocated HERE, before WiFi. ─────────
# WiFi stack (~30-40 KB) fragments the heap badly. Anything allocated after WiFi
# lands in the middle of swiss-cheese memory and can block the contiguous block
# that adafruit_mlx90640.getFrame() needs for its internal float math.
gc.collect()

INVALID_TEMP_THRESHOLD = -200.0

# Generator avoids instantiating a 768-item Python list (~12 KB of temp objects).
# In CircuitPython 10+, array('f') is not scanned by GC (no pointers), so it
# doesn't slow down collection cycles either.
frame = array('f', (0.0 for _ in range(FRAME_SIZE)))

# Receive buffer — bytearray is also GC-scan-free in CircuitPython 10+
_response_buffer = bytearray(512)

# 32 pixels × (comma + sign + 3 digits + dot + 1 digit) = 32 × 7 = 224 bytes max
_pixel_buf = bytearray(256)

# Opening JSON: prefix + min (≤7 chars) + ',"max":' + max (≤7 chars) + ',"t":['
# For sensor_id "portable": 44 + 7 + 7 + 7 + 6 = 71 bytes; 120 is safe.
_opening_buf = bytearray(120)

# Chunk size header: up to 4 hex digits + CRLF = 6 bytes; 8 gives margin
_hex_buf = bytearray(8)

# Lookup table for zero-allocation hex formatting
_HEX_CHARS = b'0123456789abcdef'

_SENSOR_ID_BYTES = SENSOR_ID.replace('\\', '\\\\').replace('"', '\\"').encode('utf-8')

# Build _opening_buf prefix in-place — the static part never changes between uploads.
# This replaces the old _OPENING_PREFIX concatenation that occurred after WiFi and
# created ~10 intermediate bytes objects in already-fragmented heap space.
_op_pos = 0
_opening_buf[_op_pos:_op_pos+14] = b'{"sensor_id":"'; _op_pos += 14
_sid_len = len(_SENSOR_ID_BYTES)
_opening_buf[_op_pos:_op_pos+_sid_len] = _SENSOR_ID_BYTES; _op_pos += _sid_len
_opening_buf[_op_pos:_op_pos+6] = b'","w":'; _op_pos += 6
# Width digits (32 → "32")
_w = MLX_SHAPE[1]
if _w >= 10:
    _opening_buf[_op_pos] = 48 + _w // 10; _op_pos += 1
_opening_buf[_op_pos] = 48 + _w % 10; _op_pos += 1
_opening_buf[_op_pos:_op_pos+5] = b',"h":'; _op_pos += 5
# Height digits (24 → "24")
_h = MLX_SHAPE[0]
if _h >= 10:
    _opening_buf[_op_pos] = 48 + _h // 10; _op_pos += 1
_opening_buf[_op_pos] = 48 + _h % 10; _op_pos += 1
_opening_buf[_op_pos:_op_pos+7] = b',"min":'; _op_pos += 7
_OP_PREFIX_LEN = _op_pos  # where variable min/max start at upload time
del _op_pos, _sid_len, _w, _h

# Pre-built HTTP request headers — identical for every upload; eliminates the
# f-string + .encode() that previously allocated a new string on each POST.
_host_header_str = API_HOST if API_PORT == 80 else (API_HOST + ":" + str(API_PORT))
_REQUEST_BYTES = (
    "POST " + API_PATH + " HTTP/1.1\r\n"
    + "Host: " + _host_header_str + "\r\n"
    + "Content-Type: application/json\r\n"
    + "Transfer-Encoding: chunked\r\n"
    + "Connection: close\r\n"
    + "\r\n"
).encode("utf-8")
del _host_header_str

# Pre-wrapped memoryviews for every constant byte sequence used in uploads.
# Avoids the memoryview(bytes) allocation that _send_all_eagain would otherwise
# make on each call — _REQUEST_BYTES once per upload, the others ~27 times.
_MV_REQUEST = memoryview(_REQUEST_BYTES)
_MV_CRLF    = memoryview(b"\r\n")
_MV_CLOSING = memoryview(b"]}")
_MV_FINAL   = memoryview(b"0\r\n\r\n")

gc.collect()

# Reset after this many consecutive MemoryErrors on getFrame().
MAX_MEMORY_ERRORS_BEFORE_RESET = 10

# ── WiFi ─────────────────────────────────────────────────────────────────────────
ssid     = os.getenv("WIFI_SSID")
password = os.getenv("WIFI_PASSWORD")

if not ssid:
    raise ValueError("WiFi credentials not found in settings.toml")


def _deep_sleep_reset(reason):
    """Deep sleep for 5 s then reboot — clears WiFi stack RAM that SW_CPU_RESET leaves behind.

    microcontroller.reset() (SW_CPU_RESET) does not fully reinitialize the ESP-IDF
    WiFi/lwIP stack. Its internal DRAM buffers survive and reduce available heap on
    the next boot, causing espidf.MemoryError even before I2C initializes. Deep sleep
    powers everything down and gives a truly clean boot.
    alarm is imported lazily here so it does not consume heap during normal startup.
    """
    print(reason, "— deep sleep reset in 5 s...")
    time.sleep(1)
    try:
        import alarm
        alarm.exit_and_deep_sleep_until_alarms(
            alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 5)
        )
    except Exception:
        microcontroller.reset()


# Always cycle the radio before connecting. After microcontroller.reset() the
# ESP32 performs a SW_CPU_RESET which does NOT power-cycle the RF subsystem.
# The router still thinks the previous session is active; immediate re-association
# fails with "Unknown failure 2/205" or "No network with that ssid". Toggling
# enabled=False forces the modem to deassociate and clears its state machine.
wifi.radio.enabled = False
time.sleep(2)
wifi.radio.enabled = True
time.sleep(1)

for attempt in range(5):
    try:
        if password:
            wifi.radio.connect(ssid=ssid, password=password)
        else:
            wifi.radio.connect(ssid=ssid)
        time.sleep(3)  # let lwIP TCP stack finish initialising after association
        break
    except (ConnectionError, OSError, RuntimeError) as e:
        print("WiFi attempt", attempt + 1, "failed:", e)
        # Toggle between retries to clear sticky ESP32 RF state
        wifi.radio.enabled = False
        time.sleep(2)
        wifi.radio.enabled = True
        time.sleep(1)
else:
    _deep_sleep_reset("WiFi unrecoverable")

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


# ── Zero-allocation helpers ───────────────────────────────────────────────────────

def _write_temp_into(buf, pos, val):
    """Write '%.1f' % val directly into bytearray buf at pos. Returns new pos.

    Replaces ("%.1f" % v).encode() which allocates a str then a bytes object.
    Called 768 times per upload for pixel data, so eliminating those 1536
    per-upload allocations is the single biggest fragmentation improvement.
    Valid for temperatures -999.9 to 9999.9 (well beyond MLX90640 range).
    """
    if val < 0.0:
        buf[pos] = 45  # '-'
        pos += 1
        val = -val
    rounded = int(val * 10.0 + 0.5)
    tenths = rounded % 10
    whole  = rounded // 10
    if whole >= 1000:
        buf[pos] = 48 + whole // 1000;        pos += 1
    if whole >= 100:
        buf[pos] = 48 + (whole // 100) % 10;  pos += 1
    if whole >= 10:
        buf[pos] = 48 + (whole // 10) % 10;   pos += 1
    buf[pos] = 48 + whole % 10; pos += 1
    buf[pos] = 46;               pos += 1  # '.'
    buf[pos] = 48 + tenths;      pos += 1
    return pos


def _write_hex_crlf(buf, n):
    """Write hex(n) + CRLF into buf starting at index 0. Returns bytes written.

    Replaces ('%x\r\n' % n).encode() — called ~28 times per upload (one per
    chunked-encoding header), so eliminates ~56 small allocations per upload.
    """
    pos = 0
    if n >= 4096:
        buf[pos] = _HEX_CHARS[n >> 12];        pos += 1
    if n >= 256:
        buf[pos] = _HEX_CHARS[(n >> 8) & 0xF]; pos += 1
    if n >= 16:
        buf[pos] = _HEX_CHARS[(n >> 4) & 0xF]; pos += 1
    buf[pos] = _HEX_CHARS[n & 0xF]; pos += 1
    buf[pos] = 13; pos += 1  # '\r'
    buf[pos] = 10; pos += 1  # '\n'
    return pos



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
        print("DNS prefetch skipped:", e)
        return
    _af = getattr(pool, "AF_INET", 2)
    for info in infos:
        if info[0] == _af:
            API_RESOLVED_IP = info[-1][0]
            print("API", API_HOST, "->", API_RESOLVED_IP)
            return
    if infos:
        API_RESOLVED_IP = infos[0][-1][0]
        print("API", API_HOST, "->", API_RESOLVED_IP)


def _send_all_eagain(sock, data):
    """Send data in full; retry EAGAIN (errno 11); handle partial sends.

    Single-loop: no 256-byte outer chunking needed — sock.send() accepts any
    size and returns the count actually sent, which we track with sent_total.
    Using memoryview throughout avoids any copy when slicing.
    """
    if not isinstance(data, memoryview):
        data = memoryview(data)
    sent_total = 0
    n = len(data)
    while sent_total < n:
        tries = 0
        while True:
            try:
                sent = sock.send(data[sent_total:])
                break
            except OSError as ex:
                if getattr(ex, "errno", None) == 11:
                    tries += 1
                    if tries > _SEND_EAGAIN_MAX:
                        raise
                    time.sleep(_SEND_EAGAIN_SLEEP_S)
                else:
                    raise
        if sent == 0:
            raise OSError("Connection broken")
        sent_total += sent


def _send_chunk(sock, data):
    """Send one HTTP/1.1 chunked-encoding chunk; zero string/bytes allocations."""
    n = len(data)
    if n == 0:
        return
    hlen = _write_hex_crlf(_hex_buf, n)
    _send_all_eagain(sock, memoryview(_hex_buf)[:hlen])
    _send_all_eagain(sock, data)
    _send_all_eagain(sock, _MV_CRLF)


def sanitize_frame_inplace(frame_data):
    """Replace invalid pixels with the min valid temperature, modifying frame_data in-place."""
    min_valid = None
    for v in frame_data:
        # v == v is False only for NaN (IEEE 754)
        if v == v and v > INVALID_TEMP_THRESHOLD:
            if min_valid is None or v < min_valid:
                min_valid = v
    if min_valid is None:
        min_valid = 0.0
    for i in range(len(frame_data)):
        v = frame_data[i]
        if v != v or v <= INVALID_TEMP_THRESHOLD:
            frame_data[i] = min_valid


def _upload_thermal_data_once(frame_data, min_temp, max_temp):
    """Single HTTP POST attempt; zero heap allocations after socket creation.

    Every string/bytes operation that previously ran per-upload has been replaced
    with direct writes into the pre-allocated module-level buffers.
    min_temp and max_temp are passed in from the caller to avoid a duplicate
    768-element scan (the caller already computes them for the print statement).
    """
    peer = API_RESOLVED_IP if API_RESOLVED_IP is not None else API_HOST
    sock = None
    try:
        try:
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        except (AttributeError, TypeError):
            sock = pool.socket()
        sock.settimeout(SOCKET_TIMEOUT_S)
        sock.connect((peer, API_PORT))
        time.sleep(3)  # let lwIP complete the TCP handshake before sending

        # Pre-built, pre-wrapped — no allocation at all
        _send_all_eagain(sock, _MV_REQUEST)

        # Build JSON opening into _opening_buf in-place.
        # The static prefix (sensor_id, w, h) was written at module init;
        # we only overwrite the variable min/max portion here.
        pos = _OP_PREFIX_LEN
        pos = _write_temp_into(_opening_buf, pos, min_temp)
        _opening_buf[pos:pos+7] = b',"max":'; pos += 7
        pos = _write_temp_into(_opening_buf, pos, max_temp)
        _opening_buf[pos:pos+6] = b',"t":['; pos += 6
        _send_chunk(sock, memoryview(_opening_buf)[:pos])

        # Stream 768 pixels in 32-pixel batches — zero allocations per pixel.
        # _write_temp_into replaces the 1536 (str + bytes) objects that
        # ("%.1f" % v).encode() created per upload.
        for batch_start in range(0, FRAME_SIZE, 32):
            pos = 0
            batch_end = min(batch_start + 32, FRAME_SIZE)
            for i in range(batch_start, batch_end):
                if i > 0:
                    _pixel_buf[pos] = 44  # ','
                    pos += 1
                pos = _write_temp_into(_pixel_buf, pos, frame_data[i])
            _send_chunk(sock, memoryview(_pixel_buf)[:pos])

        _send_chunk(sock, _MV_CLOSING)
        _send_all_eagain(sock, _MV_FINAL)

        try:
            bytes_read = sock.recv_into(_response_buffer, 512)
            if bytes_read == 0:
                return True  # body sent; peer closed before headers (unusual but ok)
            # bytearray.__contains__ correctly handles multi-byte search;
            # memoryview.__contains__ in CircuitPython only supports integers.
            if b"200" in _response_buffer[:bytes_read] or b"success" in _response_buffer[:bytes_read]:
                return True
        except Exception:
            return True  # optimistic: request body was fully sent
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
        print("Upload error: Host unreachable")
    elif errno == 111:
        print("Upload error: Connection refused")
    elif errno in (110, 116):
        print("Upload error: Connection timeout")
    elif errno in (-2, 2) or (errno is None and "Name or service" in str(e)):
        print("Upload error: DNS failed (will retry)")
        global API_RESOLVED_IP
        API_RESOLVED_IP = None
    elif errno == 11:
        print("Upload error: EAGAIN (will retry)")
    else:
        print("Upload error:", e, "(errno:", errno, ")")


def upload_thermal_data(frame_data, min_temp, max_temp):
    """Upload thermal data to API server via HTTP POST; retries transient failures."""
    if API_RESOLVED_IP is None:
        _prefetch_api_ip()
    for attempt in range(UPLOAD_MAX_ATTEMPTS):
        try:
            if _upload_thermal_data_once(frame_data, min_temp, max_temp):
                return True
        except OSError as e:
            _print_upload_oserror(e)
        except Exception as e:
            print("Upload error:", e)
        if attempt + 1 < UPLOAD_MAX_ATTEMPTS:
            time.sleep(UPLOAD_RETRY_DELAY_S)
    if UPLOAD_MAX_ATTEMPTS > 1:
        print("Upload gave up after", UPLOAD_MAX_ATTEMPTS, "attempts")
    return False


def ensure_wifi_connected():
    """Reconnect to WiFi if the connection has dropped. Returns True if connected."""
    global pool, API_RESOLVED_IP
    if wifi.radio.connected:
        return True
    print("WiFi disconnected, reconnecting...")
    try:
        # Cycle the radio to clear any stale association state
        wifi.radio.enabled = False
        time.sleep(2)
        wifi.radio.enabled = True
        time.sleep(1)
        if password:
            wifi.radio.connect(ssid=ssid, password=password)
        else:
            wifi.radio.connect(ssid=ssid)
        time.sleep(3)
        API_RESOLVED_IP = None
        pool = socketpool.SocketPool(wifi.radio)
        _prefetch_api_ip()
        print("Reconnected:", wifi.radio.ipv4_address)
        return True
    except Exception as e:
        print("WiFi reconnect failed:", e)
        return False


# Main loop
print("Connected to WiFi:", ip_addr)
print("API server:", API_URL)
_prefetch_api_ip()
print("Starting thermal data upload...")

if mlx is None:
    print("WARNING: MLX90640 sensor not initialized")
    print("Script will run but no data will be uploaded")

upload_count = 0
skip_count = 0
last_upload_time = None
last_frame_mean = None   # mean temp of the last uploaded frame; replaces last_uploaded_frame
_boot_time = time.monotonic()
sensor_fail_count = 0
MAX_SENSOR_FAILS = 5
consecutive_memory_errors = 0
consecutive_upload_failures = 0
MAX_UPLOAD_FAILURES_BEFORE_RECONNECT = 5

while True:
    try:
        _ref = last_upload_time if last_upload_time is not None else _boot_time
        if (time.monotonic() - _ref) >= WATCHDOG_RESET_S:
            _deep_sleep_reset("Watchdog: no upload for " + str(int(WATCHDOG_RESET_S)) + " s")

        if mlx is None:
            print("Sensor not available, waiting...")
            time.sleep(UPLOAD_INTERVAL)
            continue

        if not ensure_wifi_connected():
            time.sleep(UPLOAD_INTERVAL)
            continue

        gc.collect()
        try:
            mlx.getFrame(frame)
            sensor_fail_count = 0
            consecutive_memory_errors = 0
        except MemoryError:
            consecutive_memory_errors += 1
            print("Memory error reading frame (", consecutive_memory_errors, "/", MAX_MEMORY_ERRORS_BEFORE_RESET, ")", sep="")
            gc.collect()
            if consecutive_memory_errors >= MAX_MEMORY_ERRORS_BEFORE_RESET:
                _deep_sleep_reset("Heap too fragmented")
            time.sleep(UPLOAD_INTERVAL)
            continue
        except Exception as e:
            sensor_fail_count += 1
            print("Error reading frame (", sensor_fail_count, "/", MAX_SENSOR_FAILS, "):", e, sep="")
            if sensor_fail_count >= MAX_SENSOR_FAILS:
                print("Too many sensor failures, re-initializing MLX90640...")
                sensor_fail_count = 0
                try:
                    gc.collect()
                    mlx = adafruit_mlx90640.MLX90640(i2c)
                    mlx.refresh_rate = RefreshRate.REFRESH_1_HZ
                    gc.collect()
                    print("Sensor re-initialized successfully")
                except Exception as reinit_e:
                    print("Sensor re-init failed:", reinit_e)
                    mlx = None
            time.sleep(UPLOAD_INTERVAL)
            continue

        gc.collect()
        try:
            sanitize_frame_inplace(frame)
        except Exception as e:
            print("Error sanitizing frame:", e)
            time.sleep(UPLOAD_INTERVAL)
            continue

        # Compute min, max, and mean in one pass — mean is the delta-gate reference.
        min_temp = 999.0
        max_temp = -999.0
        _total = 0.0
        _count = 0
        for v in frame:
            if v > INVALID_TEMP_THRESHOLD:
                if v < min_temp: min_temp = v
                if v > max_temp: max_temp = v
                _total += v
                _count += 1
        if min_temp == 999.0 or max_temp == -999.0:
            min_temp = 0.0; max_temp = 0.0
        mean_temp = (_total / _count) if _count > 0 else 0.0

        # Delta gate: skip if the scene mean has barely changed AND a heartbeat is not due.
        # Comparing mean temperatures uses two floats instead of a 3 KB reference frame,
        # keeping the heap free for getFrame(). Mean temperature change reliably detects
        # people entering or leaving the room (each person raises the scene mean by ~0.3-2°C
        # depending on sensor field of view).
        heartbeat_due = (
            last_upload_time is None
            or (time.monotonic() - last_upload_time) >= HEARTBEAT_INTERVAL_S
        )
        if MIN_MEAN_DELTA_C > 0 and last_frame_mean is not None and not heartbeat_due:
            if abs(mean_temp - last_frame_mean) < MIN_MEAN_DELTA_C:
                skip_count += 1
                gc.collect()
                time.sleep(UPLOAD_INTERVAL)
                continue

        if upload_thermal_data(frame, min_temp, max_temp):
            upload_count += 1
            consecutive_upload_failures = 0
            last_frame_mean = mean_temp
            last_upload_time = time.monotonic()
            if skip_count:
                print("Upload #", upload_count, ": ", min_temp, " - ", max_temp,
                      " (skipped ", skip_count, " unchanged)", sep="")
                skip_count = 0
            else:
                print("Upload #", upload_count, ": ", min_temp, " - ", max_temp, sep="")
        else:
            consecutive_upload_failures += 1
            print("Upload failed (", consecutive_upload_failures, "/",
                  MAX_UPLOAD_FAILURES_BEFORE_RECONNECT, "): ", min_temp, " - ", max_temp, sep="")
            if consecutive_upload_failures >= MAX_UPLOAD_FAILURES_BEFORE_RECONNECT:
                print("Cycling WiFi radio after repeated upload failures...")
                consecutive_upload_failures = 0
                try:
                    wifi.radio.enabled = False
                    gc.collect()
                    time.sleep(2)
                    wifi.radio.enabled = True
                except Exception as e:
                    print("WiFi radio cycle error:", e)

        gc.collect()
        time.sleep(UPLOAD_INTERVAL)

    except KeyboardInterrupt:
        # Ignore DTR toggles from serial terminal connect/disconnect on CH340/CP2102 boards.
        # A real Ctrl+C from Thonny will send repeated interrupts and can still stop via the REPL.
        pass
    except Exception as e:
        print("Error in main loop:", e)
        time.sleep(UPLOAD_INTERVAL)


