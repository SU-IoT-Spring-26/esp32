"""
ESP32 CircuitPython Thermal Camera — Simple Uploader
Reads a frame from MLX90640 every UPLOAD_INTERVAL seconds and POSTs it to the
API server unconditionally. No delta gate, no heartbeat, no skip logic.

Use this as a fallback when the full uploader's delta gate or skip logic causes
confusion during debugging, or when you just need a reliable stream of frames.
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

# ── Configuration ──────────────────────────────────────────────────────────────

MLX_SHAPE   = (24, 32)
FRAME_SIZE  = MLX_SHAPE[0] * MLX_SHAPE[1]  # 768

API_URL = 'http://occupancy-api-container.yellowbush-1452fab1.canadacentral.azurecontainerapps.io/api/thermal'

SENSOR_ID       = os.getenv("SENSOR_ID", "default")
UPLOAD_INTERVAL = 15.0   # seconds between uploads
WATCHDOG_S      = 900.0  # reset if no successful upload for this long

# ── Parse API_URL once ─────────────────────────────────────────────────────────

if not API_URL.startswith("http://"):
    raise ValueError("API_URL must start with http://")
_u = API_URL[7:].split('/')
_hp = _u[0].split(':')
API_HOST = _hp[0]
API_PORT = int(_hp[1]) if len(_hp) > 1 and _hp[1] else 80
API_PATH = '/' + '/'.join(_u[1:]) if len(_u) > 1 else '/'
del _u, _hp

# ── I2C + sensor init ──────────────────────────────────────────────────────────

gc.collect()
i2c = None
try:
    i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
    gc.collect()
except MemoryError:
    try:
        import supervisor
        supervisor.reload()
    except Exception:
        microcontroller.reset()
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

gc.collect()
mlx = None
try:
    mlx = adafruit_mlx90640.MLX90640(i2c)
    mlx.refresh_rate = RefreshRate.REFRESH_1_HZ
    gc.collect()
except Exception:
    mlx = None

# ── Pre-allocate ALL buffers before WiFi ──────────────────────────────────────
# The Python heap is only ~45 KB after imports. WiFi lives in IDF DRAM so it
# does not shrink gc.mem_free(), but the allocations it triggers inside the
# Python runtime can fragment whatever free space exists at that moment.
# Allocating everything here, in order, leaves a single contiguous free block
# at the end of the heap that getFrame() can always find.
#
# Crucially, last_uploaded_frame is omitted here (no delta gate = no need),
# which reclaims 3 KB compared to the full uploader and gives getFrame() more
# room in that contiguous block.
gc.collect()

INVALID_TEMP = -200.0

frame            = array('f', (0.0 for _ in range(FRAME_SIZE)))
_response_buffer = bytearray(512)
_pixel_buf       = bytearray(256)   # 32 pixels × 7 bytes max = 224 bytes
_opening_buf     = bytearray(120)
_hex_buf         = bytearray(8)
_HEX_CHARS       = b'0123456789abcdef'

_SENSOR_ID_BYTES = SENSOR_ID.replace('\\', '\\\\').replace('"', '\\"').encode('utf-8')

# Build the static JSON prefix into _opening_buf in-place once.
_p = 0
_opening_buf[_p:_p+14] = b'{"sensor_id":"'; _p += 14
_sl = len(_SENSOR_ID_BYTES)
_opening_buf[_p:_p+_sl] = _SENSOR_ID_BYTES; _p += _sl
_opening_buf[_p:_p+6]   = b'","w":'; _p += 6
_w = MLX_SHAPE[1]
if _w >= 10: _opening_buf[_p] = 48 + _w // 10; _p += 1
_opening_buf[_p] = 48 + _w % 10; _p += 1
_opening_buf[_p:_p+5] = b',"h":'; _p += 5
_h = MLX_SHAPE[0]
if _h >= 10: _opening_buf[_p] = 48 + _h // 10; _p += 1
_opening_buf[_p] = 48 + _h % 10; _p += 1
_opening_buf[_p:_p+7] = b',"min":'; _p += 7
_OP_PREFIX_LEN = _p
del _p, _sl, _w, _h

_host_hdr = API_HOST if API_PORT == 80 else (API_HOST + ":" + str(API_PORT))
_REQUEST_BYTES = (
    "POST " + API_PATH + " HTTP/1.1\r\n"
    + "Host: " + _host_hdr + "\r\n"
    + "Content-Type: application/json\r\n"
    + "Transfer-Encoding: chunked\r\n"
    + "Connection: close\r\n"
    + "\r\n"
).encode("utf-8")
del _host_hdr

_MV_REQUEST = memoryview(_REQUEST_BYTES)
_MV_CRLF    = memoryview(b"\r\n")
_MV_CLOSING = memoryview(b"]}")
_MV_FINAL   = memoryview(b"0\r\n\r\n")

gc.collect()

# ── WiFi ───────────────────────────────────────────────────────────────────────

ssid     = os.getenv("WIFI_SSID")
password = os.getenv("WIFI_PASSWORD")
if not ssid:
    raise ValueError("WIFI_SSID not set in settings.toml")

def _deep_sleep_reset(reason):
    print(reason, "— deep sleep reset in 5 s...")
    time.sleep(1)
    try:
        import alarm
        alarm.exit_and_deep_sleep_until_alarms(
            alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 5)
        )
    except Exception:
        microcontroller.reset()

# Cycle radio before connecting — SW_CPU_RESET leaves the RF subsystem in a
# stale association state that causes "Unknown failure 2/205" on reconnect.
wifi.radio.enabled = False
time.sleep(2)
wifi.radio.enabled = True
time.sleep(1)

for _attempt in range(5):
    try:
        wifi.radio.connect(ssid=ssid, password=password)
        time.sleep(3)
        break
    except (ConnectionError, OSError, RuntimeError) as e:
        print("WiFi attempt", _attempt + 1, "failed:", e)
        wifi.radio.enabled = False
        time.sleep(2)
        wifi.radio.enabled = True
        time.sleep(1)
else:
    _deep_sleep_reset("WiFi unrecoverable")

pool = socketpool.SocketPool(wifi.radio)
gc.collect()

# ── Zero-allocation helpers ────────────────────────────────────────────────────

def _write_temp_into(buf, pos, val):
    """Write '%.1f' % val into bytearray buf at pos without allocating. Returns new pos."""
    if val < 0.0:
        buf[pos] = 45; pos += 1; val = -val
    rounded = int(val * 10.0 + 0.5)
    tenths = rounded % 10
    whole  = rounded // 10
    if whole >= 1000: buf[pos] = 48 + whole // 1000;        pos += 1
    if whole >= 100:  buf[pos] = 48 + (whole // 100) % 10;  pos += 1
    if whole >= 10:   buf[pos] = 48 + (whole // 10)  % 10;  pos += 1
    buf[pos] = 48 + whole % 10; pos += 1
    buf[pos] = 46;               pos += 1
    buf[pos] = 48 + tenths;      pos += 1
    return pos

def _write_hex_crlf(buf, n):
    """Write hex(n)+CRLF into buf[0:]. Returns bytes written."""
    pos = 0
    if n >= 4096: buf[pos] = _HEX_CHARS[n >> 12];        pos += 1
    if n >= 256:  buf[pos] = _HEX_CHARS[(n >> 8) & 0xF]; pos += 1
    if n >= 16:   buf[pos] = _HEX_CHARS[(n >> 4) & 0xF]; pos += 1
    buf[pos] = _HEX_CHARS[n & 0xF]; pos += 1
    buf[pos] = 13; pos += 1
    buf[pos] = 10; pos += 1
    return pos

def _send_all(sock, data):
    """Send data in full, retrying EAGAIN (errno 11)."""
    if not isinstance(data, memoryview):
        data = memoryview(data)
    sent = 0
    n = len(data)
    while sent < n:
        tries = 0
        while True:
            try:
                s = sock.send(data[sent:]); break
            except OSError as ex:
                if getattr(ex, 'errno', None) == 11:
                    tries += 1
                    if tries > 200: raise
                    time.sleep(0.1)
                else:
                    raise
        if s == 0: raise OSError("Connection broken")
        sent += s

def _send_chunk(sock, data):
    n = len(data)
    if n == 0: return
    hlen = _write_hex_crlf(_hex_buf, n)
    _send_all(sock, memoryview(_hex_buf)[:hlen])
    _send_all(sock, data)
    _send_all(sock, _MV_CRLF)

def _sanitize(frame_data):
    """Replace NaN and below-threshold values with the minimum valid temperature."""
    mn = None
    for v in frame_data:
        if v == v and v > INVALID_TEMP:
            if mn is None or v < mn: mn = v
    if mn is None: mn = 0.0
    for i in range(len(frame_data)):
        v = frame_data[i]
        if v != v or v <= INVALID_TEMP:
            frame_data[i] = mn

def _upload(frame_data, min_temp, max_temp):
    """One HTTP POST attempt. Returns True on success."""
    sock = None
    try:
        try:
            sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        except (AttributeError, TypeError):
            sock = pool.socket()
        sock.settimeout(20.0)
        sock.connect((API_HOST, API_PORT))
        time.sleep(3)  # lwIP on ESP32 returns from connect() before handshake completes

        _send_all(sock, _MV_REQUEST)

        pos = _OP_PREFIX_LEN
        pos = _write_temp_into(_opening_buf, pos, min_temp)
        _opening_buf[pos:pos+7] = b',"max":'; pos += 7
        pos = _write_temp_into(_opening_buf, pos, max_temp)
        _opening_buf[pos:pos+6] = b',"t":['; pos += 6
        _send_chunk(sock, memoryview(_opening_buf)[:pos])

        for batch_start in range(0, FRAME_SIZE, 32):
            pos = 0
            for i in range(batch_start, min(batch_start + 32, FRAME_SIZE)):
                if i > 0: _pixel_buf[pos] = 44; pos += 1
                pos = _write_temp_into(_pixel_buf, pos, frame_data[i])
            _send_chunk(sock, memoryview(_pixel_buf)[:pos])

        _send_chunk(sock, _MV_CLOSING)
        _send_all(sock, _MV_FINAL)

        try:
            n = sock.recv_into(_response_buffer, 512)
            if n == 0 or b"200" in _response_buffer[:n] or b"success" in _response_buffer[:n]:
                return True
        except Exception:
            return True  # body was sent; optimistic
        return False
    finally:
        if sock is not None:
            try: sock.close()
            except Exception: pass

def _ensure_wifi():
    """Reconnect if WiFi dropped. Returns True if connected."""
    global pool
    if wifi.radio.connected:
        return True
    print("WiFi dropped, reconnecting...")
    try:
        wifi.radio.enabled = False; time.sleep(2)
        wifi.radio.enabled = True;  time.sleep(1)
        wifi.radio.connect(ssid=ssid, password=password)
        time.sleep(3)
        pool = socketpool.SocketPool(wifi.radio)
        print("Reconnected:", wifi.radio.ipv4_address)
        return True
    except Exception as e:
        print("WiFi reconnect failed:", e)
        return False

# ── Main loop ──────────────────────────────────────────────────────────────────

print("Connected:", wifi.radio.ipv4_address)
print("Uploading to:", API_URL)
if mlx is None:
    print("WARNING: sensor not initialised — no data will be sent")

upload_count      = 0
sensor_fails      = 0
_boot_time        = time.monotonic()
last_upload_time  = None

while True:
    # Watchdog: reset if no successful upload for WATCHDOG_S seconds.
    _ref = last_upload_time if last_upload_time is not None else _boot_time
    if time.monotonic() - _ref >= WATCHDOG_S:
        _deep_sleep_reset("Watchdog: no upload for " + str(int(WATCHDOG_S)) + " s")

    if mlx is None:
        time.sleep(UPLOAD_INTERVAL)
        continue

    if not _ensure_wifi():
        time.sleep(UPLOAD_INTERVAL)
        continue

    gc.collect()
    try:
        mlx.getFrame(frame)
        sensor_fails = 0
    except MemoryError:
        print("MemoryError on getFrame — resetting")
        _deep_sleep_reset("getFrame MemoryError")
        continue
    except Exception as e:
        sensor_fails += 1
        print("Sensor read error (", sensor_fails, "):", e, sep="")
        if sensor_fails >= 5:
            print("Re-initialising sensor...")
            sensor_fails = 0
            try:
                gc.collect()
                mlx = adafruit_mlx90640.MLX90640(i2c)
                mlx.refresh_rate = RefreshRate.REFRESH_1_HZ
                gc.collect()
            except Exception as re:
                print("Re-init failed:", re); mlx = None
        time.sleep(UPLOAD_INTERVAL)
        continue

    _sanitize(frame)

    min_temp = 999.0; max_temp = -999.0
    for v in frame:
        if v > INVALID_TEMP:
            if v < min_temp: min_temp = v
            if v > max_temp: max_temp = v
    if min_temp == 999.0: min_temp = 0.0
    if max_temp == -999.0: max_temp = 0.0

    try:
        ok = _upload(frame, min_temp, max_temp)
    except Exception as e:
        print("Upload error:", e)
        ok = False

    if ok:
        upload_count += 1
        last_upload_time = time.monotonic()
        print("Upload #", upload_count, ": ", min_temp, " - ", max_temp, sep="")
    else:
        print("Upload failed")

    gc.collect()
    time.sleep(UPLOAD_INTERVAL)
