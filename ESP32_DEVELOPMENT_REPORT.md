# ESP32 Thermal Camera Uploader — Development Report

## Overview

This document records the full development history of `mlx90640_uploader.py`, a CircuitPython script that runs on an ESP32 DevKit V1 fitted with an MLX90640 32×24 thermal camera. The device continuously reads thermal frames from the sensor and uploads them over HTTP to a cloud-hosted FastAPI backend on Azure Container Apps, where they are used for occupancy estimation.

The development involved a long series of hardware-driven debugging challenges. Most were caused by the extreme memory constraints of the CircuitPython runtime on ESP32, combined by non-obvious behaviors of the lwIP TCP stack and the MLX90640 sensor library. This report describes each problem, what was tried, what failed and why, and what ultimately worked.

---

## Hardware and Runtime Environment

- **Device:** ESP32 DevKit V1 (CH340/CP2102 USB-serial chip)
- **Sensor:** MLX90640 32×24 thermal camera via I2C
- **Runtime:** CircuitPython 10.0.3
- **Backend:** FastAPI on Azure Container Apps (HTTP, no TLS on device)
- **Available heap:** approximately 100–120 KB at startup, before any imports

CircuitPython's garbage collector is **non-compacting and mark-sweep only**. When an object is freed, it leaves a hole in the heap at its original address. Subsequent `gc.collect()` calls mark and free unreachable objects but do not move live objects to consolidate free space. This means fragmentation is permanent: once a pattern of variably-sized holes exists, there is no way to recover contiguous free space short of a reboot. This property was the root cause of the majority of problems encountered.

---

## Phase 1 — Initial Implementation

The first version of the uploader was straightforward: read a thermal frame into a Python list, format it as JSON using string concatenation, and POST it via a raw TCP socket. The initial code had a number of issues that surfaced during early testing:

- **String concatenation in JSON generation** built the JSON payload by concatenating strings in a loop 768 times. Each `+=` on a string allocated a new string object, copied the growing result, and left the old one to be collected. Over 768 iterations this created and discarded approximately 1,500 heap objects and triggered repeated reallocation of increasingly large string buffers. This caused heap fragmentation that accumulated over hours of continuous operation and eventually led to crashes.
- **No WiFi reconnect logic** meant that if the WiFi association dropped, the script would fail silently and stop uploading indefinitely without any recovery.
- **No delta gate** meant every 15-second poll resulted in an upload, even when the scene was completely static.

These were addressed incrementally: the JSON generation was rewritten to use `bytearray` with `list.append()` and a final `b''.join()`, WiFi reconnect logic was added, and a mean-absolute-delta gate was introduced to skip uploads when the scene had not changed by more than a configurable threshold.

---

## Phase 2 — WiFi and Socket Reliability

After the initial fixes, the device connected to WiFi and began uploading. Several new issues appeared:

**Open-network WiFi connection failure.** The university WiFi network has no password. Passing an empty string as the `password` argument to `wifi.radio.connect()` caused a `ConnectionError` on CircuitPython even though the network required no credentials. The fix was to call `wifi.radio.connect(ssid=ssid)` with no password argument at all when no password was configured.

**Socket non-blocking behavior from `settimeout()`.** Setting a timeout on a socket via `sock.settimeout()` silently placed it into non-blocking mode on some CircuitPython builds, causing `recv()` and `send()` to raise `OSError: EAGAIN` (errno 11) immediately instead of waiting. This was replaced with `setblocking(True)` to restore synchronous behavior, though this was later revised again when the full EAGAIN picture became clearer (see Phase 3).

**`wifi.radio.connected` lying.** After extended operation, `wifi.radio.connected` could return `True` even when the connection had stopped routing traffic — the device was still associated with the access point at layer 2 but had no functional network path. Uploads would time out or fail indefinitely without the script knowing to reconnect. A counter of consecutive upload failures was added: after five consecutive failures, the script forcibly cycles `wifi.radio.enabled` off and back on, which causes a full reassociation and forces `ensure_wifi_connected()` to do a clean reconnect.

**DTR toggle killing the main loop.** Opening a serial terminal in Thonny toggles the DTR line on the CH340/CP2102 USB-serial chip. CircuitPython interprets a DTR toggle as a `KeyboardInterrupt`, which propagated up to the top of the main loop and exited it. The fix was to catch `KeyboardInterrupt` at the outermost `try/except` with `pass` instead of `break`, so that plugging in a serial monitor does not terminate the uploader.

---

## Phase 3 — EAGAIN Upload Failures After WiFi Connect

After the WiFi and socket fixes were in place, a new class of failure appeared in the serial output:

```
DBG: connect OK
Upload error: EAGAIN (will retry)
Upload error: EAGAIN (will retry)
Upload error: EAGAIN (will retry)
Upload gave up after 3 attempts
```

The failure was `OSError: EAGAIN` (errno 11, "resource temporarily unavailable") on the first `sock.send()` call immediately after a successful `sock.connect()`. This initially appeared to contradict the `setblocking(True)` fix from Phase 2.

**Root cause: lwIP TCP handshake timing.** CircuitPython's `sock.connect()` call on ESP32 returns control to Python as soon as the TCP SYN packet has been sent, before the SYN-ACK response from the server has been received and before the 3-way handshake is complete. The socket is technically "connected" at the CircuitPython level, but the underlying lwIP stack has not yet completed the handshake. Calling `sock.send()` at this point returns EAGAIN because the socket's send buffer is not yet ready to accept data.

**Attempted fix 1: increase EAGAIN retry count.** The initial retry loop retried EAGAIN up to 10 times with a 50ms sleep between attempts. Increasing this to 200 retries with 100ms sleep did not fix the problem — the EAGAIN errors were occurring on the very first send, before even the first retry, and the retry count had no bearing on the root cause.

**Attempted fix 2: re-examine `setblocking`.** Re-reading the CircuitPython socket documentation revealed that `settimeout()` versus `setblocking()` was not the issue. The problem was purely the timing gap between `connect()` returning and the handshake completing.

**What worked: `time.sleep(3)` after connect.** Adding a 3-second pause after `sock.connect()` before any `sock.send()` call gave the lwIP stack enough time to complete the handshake in all tested conditions. This was confirmed by adding diagnostic print statements that showed the full sequence: `connect OK → [sleep] → headers sent → body sent → Upload #1 success`. The sleep value was empirically determined; 1 second was insufficient on the Azure endpoint due to transatlantic round-trip time, while 3 seconds was reliable.

This sleep was later questioned by an external review (see Phase 6), which claimed `sock.connect()` was blocking in CircuitPython. This claim was incorrect for this hardware/firmware combination. The sleep was retained.

---

## Phase 4 — Persistent MemoryError After the First Upload

With uploads now succeeding, a new and more difficult problem emerged. The device would complete exactly one upload successfully, then print `Memory error reading frame, retrying...` on every subsequent loop iteration, uploading nothing further:

```
Connected to WiFi: 10.0.0.116
Upload #1: 22.8°C - 26.7°C
Memory error reading frame, retrying...
Memory error reading frame, retrying...
Memory error reading frame, retrying...
```

The error was a `MemoryError` raised inside `mlx.getFrame(frame)`, not on the JSON generation or upload. The first call to `getFrame()` always succeeded; every subsequent call failed. This pattern — working exactly once, then failing forever — pointed directly to the non-compacting garbage collector: the first upload was permanently fragmenting the heap in a way that left no sufficiently large contiguous block for `getFrame()` to use on the next iteration.

This problem required many attempts to solve. Each attempt was based on a hypothesis about what was consuming heap, which was then refined as each fix failed.

### Understanding what `getFrame()` needs

The `adafruit_mlx90640` library uses I2C to read raw calibration and pixel data from the sensor in large bursts. Internally it allocates several buffers for the I2C read. These buffers require **contiguous heap blocks** — the GC cannot satisfy an allocation from two adjacent but separately-freed regions. The available user pre-allocation budget before `getFrame()` fails appears to be approximately 3.5–6 KB of additional contiguous heap beyond what WiFi and the runtime already consume.

### Attempt 1 — Pre-allocate all buffers at startup

**Hypothesis:** Allocate `_json_buf`, `_sanitized_frame_buf`, and `_last_uploaded_frame_buf` at module level before WiFi connects, so the heap is cleanly partitioned and all large blocks are in place before `getFrame()` ever runs.

Three 768-element Python float lists plus a 5120-byte bytearray were allocated at startup. **Result:** `getFrame()` failed on the very first call. Pre-allocating ~26 KB of Python float lists before WiFi left no room for `getFrame()` to allocate its I2C buffers at all. The hypothesis was wrong about the cost — Python float lists are expensive.

### Understanding Python float list layout

A Python list `[0.0] * 768` does not store 768 float values inline. It stores 768 **pointers** to Python float objects, each of which is a separately heap-allocated 16-byte object. However, `[0.0] * 768` creates exactly **one** `float(0.0)` object and 768 references to the same object — the list costs approximately 3 KB (768 × 4-byte pointers), not 12 KB. After `mlx.getFrame(frame)` writes real temperatures into the list, those 768 shared references are replaced with 768 distinct Python float objects, and the list grows from ~3 KB to ~15 KB. This growth was the source of most subsequent incorrect cost estimates.

### Attempt 2 — Switch frame buffers to `array('f')`

**Hypothesis:** Using `array('f', [0.0] * FRAME_SIZE)` instead of a Python list would store raw float32 values at 4 bytes each, making the frame buffer 3 KB instead of 15 KB after `getFrame()` fills it.

**Result:** `MemoryError` on the very first `getFrame()` call. The `adafruit_mlx90640` library's `getFrame()` method internally calls methods that expect a plain Python list and performs operations incompatible with `array('f')`. Passing an array type caused the library to fail during its first call. This required reverting `frame` back to a plain Python list.

**What was kept:** `last_uploaded_frame` continued to be stored as `array('f')` after each successful upload (since it is only ever read for the delta comparison, not passed to the library). This saved ~12 KB of persistent heap between uploads.

### Attempt 3 — Lazy `_json_buf` allocation

**Hypothesis:** If `_json_buf` is `None` at startup and allocated only inside `generate_thermal_json()` on first call (after `getFrame()` has run), the 6 KB buffer would not be present during `getFrame()`.

**Result:** Upload #1 succeeded. Upload #2: `MemoryError` on `getFrame()`. The lazy allocation meant `_json_buf` was allocated after the first `getFrame()`, persisted through the first upload, and was still present when the second `getFrame()` ran. The 6 KB contiguous block on the heap was now occupied, and `getFrame()` could not find what it needed.

### Attempt 4 — Sanitize in-place instead of building a new list

**Hypothesis:** `sanitize_frame()` was building a new 768-element list via 768 `.append()` calls. Python lists grow dynamically, triggering approximately 7–8 internal reallocation-and-copy cycles as the list expands. Each reallocation leaves a dead block of the previous size (64 B, 128 B, 256 B, 512 B, 1 KB, 2 KB, 4 KB) permanently in the heap. These scattered dead blocks were preventing `getFrame()` from finding a contiguous I2C read buffer on the next call.

The fix was `sanitize_frame_inplace()`, which modifies the frame array in-place with no new allocation at all.

**Result:** Upload #1 succeeded. Upload #2: `MemoryError` on `getFrame()`. In-place sanitization eliminated the fragmentation from the sanitizer, but `getFrame()` was still failing. The root problem had not been fully solved.

### Root cause finally understood

At this point, the full picture became clear. The key constraint is:

- The user heap available for pre-allocation before the first `getFrame()` is approximately **3.5–6 KB maximum** (beyond what WiFi and the runtime already use)
- `frame = [0.0] * FRAME_SIZE` allocates approximately 3 KB (one shared float, 768 pointers) — this fits
- `_json_buf = bytearray(6144)` allocates 6 KB — this exceeds the budget by itself when combined with the frame arrays

No matter when `_json_buf` was allocated relative to `getFrame()`, it needed to be absent from the heap whenever `getFrame()` ran. The lazy allocation approach kept `_json_buf` alive across the entire loop iteration — allocated before upload, persisting through the 15-second sleep, still present for the next `getFrame()`. Freeing it explicitly after each upload and reallocating it before the next upload could work in principle, but would itself cause fragmentation from repeated 6 KB allocations.

---

## Phase 5 — Solution: HTTP Chunked Transfer Encoding

The solution was to eliminate `_json_buf` entirely by streaming the JSON payload directly into the socket as it is generated, using HTTP/1.1 `Transfer-Encoding: chunked`. Under this scheme, there is no pre-built JSON string. Instead:

1. The HTTP request headers are sent first, with `Transfer-Encoding: chunked` instead of `Content-Length`.
2. The JSON opening (sensor metadata, ~100 bytes) is sent as one chunk.
3. The 768 pixel values are formatted and sent in 24 batches of 32 pixels, each batch using a single reused 288-byte local `bytearray`. Each batch is sent as one chunk.
4. The JSON closing `]}` is sent as a final chunk, followed by the chunked terminator `0\r\n\r\n`.

The HTTP/1.1 chunked encoding format wraps each piece of data with a hex-encoded length prefix and a trailing `\r\n`. The server's HTTP stack (uvicorn) assembles the full body before the FastAPI handler sees it, so the application layer receives a complete JSON document transparently.

**Memory impact:** Peak RAM during the upload dropped from 6,144 bytes (`_json_buf`) to approximately 288 bytes (one pixel batch buffer). The 288-byte buffer is allocated after `getFrame()` has returned, and freed when the upload function returns. At the moment `getFrame()` runs, the only user pre-allocations on the heap are the two `array('f')` frame buffers (6 KB total) and the 512-byte response buffer.

**Result:** The `MemoryError` was eliminated. `getFrame()` succeeded on every iteration.

The pixel batch buffer was sized at 288 bytes (not the initially obvious 256) to provide headroom against worst-case temperature formatting. With 32 pixels per batch, each value up to 8 characters (e.g. "-1000.0") plus a comma, the maximum possible batch is 32 × 9 = 288 bytes. For realistic MLX90640 temperatures (−40°C to 300°C), the actual maximum is well below 224 bytes per batch.

---

## Phase 6 — Additional Hardening

After the chunked streaming fix, several additional improvements were made based on code review:

**NaN from sensor noise (critical fix).** The MLX90640 sensor occasionally produces `NaN` (Not a Number) floating-point values due to sensor noise or numerical overflow in the emissivity calibration math inside the library. Due to IEEE 754 semantics, `NaN <= -200.0` evaluates to `False`, which means NaN values silently bypass the `INVALID_TEMP_THRESHOLD` check in `sanitize_frame_inplace()`. When `"%.1f" % float('nan')` is evaluated in CircuitPython, it produces the literal string `"nan"`. The JSON specification does not permit `nan` as a value, and standard JSON parsers (including Python's `json` module and FastAPI's request parsing) raise an exception when they encounter it, crashing the API endpoint for that request.

The fix uses the IEEE 754 identity that `NaN` is the only value not equal to itself: `v != v` is `True` only for `NaN`. This allows NaN detection without importing the `math` module (which would consume additional heap). The sanitizer now checks `if v == v and v > INVALID_TEMP_THRESHOLD` to find valid pixels, and replaces any pixel where `v != v or v <= INVALID_TEMP_THRESHOLD`.

**Redundant `None` checks removed.** The original `sanitize_frame_inplace()` included `if v is not None` and `if frame_data[i] is None` guards. Since `frame` is now `array('f')`, which is a typed C-level array that can only hold raw float values, these checks can never be true and were removed as dead code.

**Zero-copy sends with `memoryview`.** In `_send_all_eagain()`, the inner loop sliced the data buffer with `chunk = data[total:end]`. When `data` is a `bytes` object (as it is for the HTTP headers and JSON fragments), slicing `bytes` allocates a new `bytes` object on the heap for every 256-byte chunk. Wrapping `data` in a `memoryview` at the top of the function means that slicing creates a zero-allocation view into the original buffer instead of copying it. This eliminates repeated small heap allocations during every send operation.

**I2C bus frequency.** The MLX90640 sensor requires fast I2C to sustain 4 Hz refresh rate. CircuitPython's `board.I2C()` singleton initialises at 100 kHz (standard mode), which is too slow for the sensor's data rate. The code explicitly creates the I2C bus at 400 kHz (fast mode). If the bus is already in use when the script starts (e.g. from a previous run that did not deinitialise it), the code catches the `ValueError: I2C in use`, deinitialises the singleton via `board.I2C().deinit()`, and reinitialises at 400 kHz. A naive fallback to `board.I2C()` would silently reinitialise at 100 kHz, which would cause frame read failures at 4 Hz.

**Buffer overflow protection.** The 6 KB `_json_buf` from earlier versions was sized at 5120 bytes initially, which was too small. With 768 pixels and a worst-case temperature like `"-199.9"` (6 characters), plus commas and a ~200-byte JSON header, the payload can reach 5376 bytes, overflowing the buffer silently (corrupting adjacent heap memory). This was corrected to 6144 bytes in the intermediate versions. With the chunked streaming approach, this concern was eliminated entirely — no large buffer exists.

**`request` string freed early.** Inside `_upload_thermal_data_once()`, the HTTP request headers string (~200 bytes) was kept alive as a local variable for the entire duration of the function. Adding `del request` immediately after encoding and sending it frees the memory before the pixel-streaming phase begins.

---

## Summary of What Failed and Why

| Attempt | What Was Tried | Why It Failed |
|---|---|---|
| String `+=` JSON | Build JSON via string concatenation loop | 1500+ allocs, permanent heap fragmentation |
| `list.append()` + `join()` | Build JSON token list, join at end | Still fragmented heap over time |
| Pre-allocate all buffers | Allocate all buffers at startup | 26 KB startup consumption; `getFrame()` had no room |
| `array('f')` for frame | Typed array instead of Python list | `adafruit_mlx90640.getFrame()` requires a Python list |
| Lazy `_json_buf` | Allocate 6 KB buffer on first use | Buffer persisted across 15-second sleep; present when next `getFrame()` ran |
| In-place sanitization alone | Remove list-growing sanitizer | Eliminated fragmentation from sanitizer but `_json_buf` was still the blocker |
| 3s sleep removal | Reduce or eliminate post-connect sleep | EAGAIN failures on `sock.send()` immediately after connect — TCP handshake not complete |

## Summary of What Worked

| Fix | Mechanism | Impact |
|---|---|---|
| HTTP chunked transfer | Stream JSON in 32-pixel batches; no `_json_buf` | Eliminated `MemoryError` on all subsequent `getFrame()` calls |
| `array('f')` for `last_uploaded_frame` | Typed C-level array vs Python float objects | 12 KB savings on persistent per-upload heap |
| `sanitize_frame_inplace()` | Modify frame in-place, no new list | Eliminated 7–8 realloc dead blocks per iteration |
| `time.sleep(3)` after connect | Wait for lwIP handshake before first send | Eliminated EAGAIN failures on TCP send |
| NaN detection via `v != v` | IEEE 754 identity, no `math` import | Prevents invalid JSON reaching the API parser |
| `memoryview` in `_send_all_eagain` | View instead of slice-copy | Eliminates small heap allocs on every send chunk |
| `del request` after send | Explicit free of 200-byte string | Frees memory before pixel-streaming phase |
| WiFi radio cycle | `enabled = False` then `True` | Recovers from "associated but not routing" stale connection |
| I2C at 400 kHz with deinit fallback | Explicit frequency; handle `in use` | Correct sensor timing; survives script restart without power cycle |
| `_SENSOR_ID_BYTES` pre-encoded | Encode sensor ID once at module level | Avoids repeated string allocation in the hot upload path |
