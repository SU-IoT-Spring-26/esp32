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

---

## Phase 7 — `espidf.MemoryError` at I2C Init After Watchdog Reset

With the chunked streaming fix in place, the device ran reliably for extended periods. A new failure mode then appeared. When the watchdog triggered (no successful upload for 900 seconds), the script called `microcontroller.reset()` to reboot. On the next boot, the device crashed with a `MemoryError` inside `busio.I2C()` before any user buffers had been allocated — before WiFi, before imports, before almost anything. The serial output showed:

```
Traceback (most recent call last):
  File "code.py", line 59, in <module>
espidf.MemoryError: memory allocation failed, allocating 360 bytes
```

Line 59 is the `busio.I2C(board.SCL, board.SDA, frequency=400000)` call. The allocation of 360 bytes — a tiny amount — was failing. This was paradoxical: the device had just been running with megabytes of flash and apparently enough RAM to run the full script.

**Root cause: SW_CPU_RESET does not power-cycle the ESP-IDF WiFi stack.** `microcontroller.reset()` in CircuitPython performs an ESP-IDF `esp_restart()`, which maps to a software CPU reset (`SW_CPU_RESET`). This resets the CPU and restarts the firmware, but it does not power-cycle the RF subsystem or release the IDF internal DRAM buffers that the WiFi stack allocated during the previous session. These buffers — approximately 35–40 KB — persist across a software reset, leaving the Python heap 35–40 KB smaller on every reboot via `microcontroller.reset()` than on a cold boot. Since the Python heap is only approximately 100–120 KB after the ESP-IDF runtime itself, losing 35–40 KB to stale WiFi buffers left insufficient room for even a 360-byte I2C allocation.

**Fix: replace `microcontroller.reset()` with deep sleep.** `alarm.exit_and_deep_sleep_until_alarms()` causes the ESP32 to fully power down, including the RF subsystem. When the device wakes from deep sleep, the IDF WiFi stack is completely uninitialized and its DRAM has been released. The heap on wake is identical to a cold boot. The `_deep_sleep_reset()` helper was added to encapsulate this pattern, with a 5-second delay to give any in-progress operations time to finish, and a `microcontroller.reset()` fallback for environments where the `alarm` module is unavailable.

The `alarm` module is imported lazily inside `_deep_sleep_reset()` rather than at the top of the script. A module-level import would add the module to the heap for the entire lifetime of the script, consuming heap space on every run even though the reset path is rarely taken.

---

## Phase 8 — Persistent MemoryError After Optimizations (Diagnostic Investigation)

After the deep sleep fix, the device still produced `Memory error reading frame` on every iteration except the first. To understand exactly what was consuming heap, `gc.mem_free()` diagnostic prints were added at each stage of initialization. The output revealed the actual heap budget:

```
mem/imports:  44960   # only 45 KB free after all imports
mem/MLX:      44416   # MLX object costs 544 bytes
mem/bufs:     39904   # pre-allocated buffers cost ~4.5 KB
mem/WiFi:     39680   # WiFi lives in IDF DRAM — barely touches Python heap
mem/getFrame: 39152   → Upload #1: SUCCESS
mem/getFrame: 36064   → Memory error reading frame (1/10)
mem/getFrame: 35856   → Memory error reading frame (still failing after GC)
```

Several discoveries from this output:

**The Python heap is only 45 KB after imports, not 100–120 KB.** The 100–120 KB figure was the total DRAM available before any Python runtime initialization. After loading CircuitPython, all library modules, the WiFi stack, and the runtime itself, only 45 KB remained for user allocations. Every estimate in Phases 1–6 about "room" for buffers had been based on an incorrect figure.

**WiFi does not consume Python heap.** The WiFi stack (`wifi.radio.connect()`) uses IDF DRAM, which is in a separate memory region not tracked by `gc.mem_free()`. This explained why the pre-WiFi allocation strategy had seemed wrong in earlier phases — checking `gc.mem_free()` before and after WiFi connection showed almost no change, yet the heap was still fragmented after connecting.

**`last_uploaded_frame` was the culprit.** The version under test had `last_uploaded_frame = None` at startup (not pre-allocated). The array was allocated lazily inside the upload function on first successful upload: `last_uploaded_frame = array('f', frame)`. This 3 KB allocation happened after the first `getFrame()` had run and after the upload's socket temporaries had been freed. The result was a 3 KB array inserted into the middle of a heap that now had small holes punched through it by socket and send-buffer temporaries. On the second iteration, `getFrame()` needed a contiguous block of approximately 3–4 KB for its internal I2C read buffers, but the 3 KB `last_uploaded_frame` now occupied the only contiguous block of that size. `gc.collect()` could not help — `last_uploaded_frame` was still alive and could not be moved.

The fix: pre-allocate `last_uploaded_frame` before WiFi, identical to all other large buffers. This places the 3 KB array at a predictable location in the heap before fragmentation begins, leaving a single contiguous free block at the high end of the heap that `getFrame()` can reliably use.

---

## Phase 9 — `mlx90640_simple.py`: A Reliable Baseline

To establish a guaranteed-working reference while continuing to debug the full uploader, a stripped-down version called `mlx90640_simple.py` was created. This version:

- Uploads every frame unconditionally, every 15 seconds, with no delta gate
- Has no `last_uploaded_frame` buffer at all (saving 3 KB vs the full uploader)
- Uses all the memory optimizations developed in Phases 4–6: chunked streaming, `array('f')` for the frame buffer, `_write_temp_into()`, `_write_hex_crlf()`, `_REQUEST_BYTES` pre-built, `memoryview` wrapping
- Uses deep sleep for all resets
- Has a single upload attempt per cycle (no retry)

This version was deployed to the device and ran for 2,042 successful uploads without a single MemoryError, confirming that the optimizations were correct and that the delta gate's `last_uploaded_frame` was the remaining source of instability.

---

## Phase 10 — Mean-Based Delta Gate (Final Architecture)

With a working baseline established, the goal was to reintroduce the delta gate without reintroducing `last_uploaded_frame`. The key insight was that occupancy detection does not require comparing every pixel between frames. It only needs to detect significant thermal changes in the scene — people entering or leaving. A person entering a room raises the scene's mean temperature by approximately 0.3–2°C depending on field of view and sensor mounting height. Monitoring the mean temperature change requires storing exactly **one float** (`last_frame_mean`) rather than a 768-float array (3 KB).

The revised algorithm:
- On each frame, compute mean temperature in the same pass as min/max (no extra iteration)
- If `|mean_temp - last_frame_mean| < MIN_MEAN_DELTA_C` and a heartbeat is not due, skip the upload
- Otherwise upload and update `last_frame_mean`
- Heartbeat: force an upload at least every `HEARTBEAT_INTERVAL_S` seconds regardless of the delta, so the API always receives fresh data even in a static, empty room

**Heartbeat interval calibration.** An initial heartbeat interval of 600 seconds (10 minutes) was tried. In a static room, the mean temperature barely drifts, so almost every frame is skipped and the heartbeat becomes the only upload path. At 10 minutes between uploads, the device appeared to be malfunctioning — the dashboard would show stale data for long periods and the watchdog threshold (900 seconds) was uncomfortably close to the heartbeat interval. The heartbeat was reduced to **60 seconds**, which provides regular uploads in static rooms while still reducing traffic approximately 4× compared to uploading every frame.

---

## Phase 11 — Bug Review of the Final `code.py`

Before finalizing the code, a systematic comparison of the device's running `code.py` against the known-good `mlx90640_simple.py` revealed three bugs:

**Bug 1: `HEARTBEAT_INTERVAL_S = 600.0`** — the 10-minute value from the initial attempt (see Phase 10) had not been changed after the interval was identified as too long. This was the direct cause of only 1 upload being produced during testing. Fixed to `60.0`.

**Bug 2: `_deep_sleep_reset` called before it was defined.** The initial WiFi connection loop at module level (lines ~180–196) called `_deep_sleep_reset("WiFi unrecoverable")` in its `else` clause if all 5 attempts failed. The `_deep_sleep_reset` function was defined later in the file, at approximately line 214. In Python, a `def` statement is executed when the interpreter reaches it during module loading; the function does not exist in the namespace until then. If all 5 WiFi attempts failed during the initial connection loop, the `else` clause would raise `NameError: name '_deep_sleep_reset' is not defined` instead of performing the reset. The fix was to move `_deep_sleep_reset` above the WiFi block.

**Bug 3: `bytearray` slice off-by-one in `_opening_buf` construction.** The line:
```python
_opening_buf[_op_pos:_op_pos+6] = b',"h":'; _op_pos += 5  # note: 5 not 6
```
assigns a 5-byte bytes literal to a 6-byte slice. In MicroPython/CircuitPython, assigning a shorter bytes object to a longer slice shrinks the bytearray by 1 byte. The `_opening_buf` buffer would become 119 bytes instead of 120. The `_op_pos` counter was correct (incremented by 5), so subsequent writes would land in the right positions, but the buffer's total length was permanently reduced. With 120-byte margin this caused no observable failure, but it was still a latent error. Fixed to `_opening_buf[_op_pos:_op_pos+5] = b',"h":'`.

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
| Mean-based delta gate | One float `last_frame_mean` vs 3 KB `last_uploaded_frame` | Eliminates the heap fragmentation that caused MemoryError after first upload |
| `HEARTBEAT_INTERVAL_S = 60` | Force upload every 60 s in static room | Provides regular data flow without appearing broken; safe distance from 900 s watchdog |
| `_deep_sleep_reset()` for all resets | `alarm.exit_and_deep_sleep_until_alarms()` instead of `microcontroller.reset()` | Prevents `espidf.MemoryError` at I2C init caused by stale IDF WiFi DRAM after SW_CPU_RESET |
| `_deep_sleep_reset` defined before WiFi block | Move function def above its first call site | Prevents `NameError` if all 5 initial WiFi attempts fail |

---

## Final Architecture: `code.py` Safeguards

The final version of `code.py` (also mirrored as `mlx90640_uploader.py`) provides reliable continuous operation through the following layered safeguards:

### Memory Safety
- All large buffers (`frame`, `_response_buffer`, `_pixel_buf`, `_opening_buf`, `_hex_buf`, `_REQUEST_BYTES`, all `memoryview` wrappers) are allocated at module level **before** WiFi connects, preserving a single contiguous free block that `getFrame()` can always use.
- The delta gate uses one float (`last_frame_mean`) rather than a 768-float reference frame, keeping the allocation budget 3 KB smaller than the original design.
- JSON is streamed in 32-pixel batches via HTTP chunked transfer encoding — no large JSON string is ever constructed on the heap.
- Zero-allocation helpers `_write_temp_into()` and `_write_hex_crlf()` replace `("%.1f" % v).encode()` and `('%x\r\n' % n).encode()`, eliminating approximately 1,600 per-upload heap allocations.
- `memoryview` is used throughout `_send_all_eagain()` so that slicing the send buffer never copies bytes.
- The `alarm` module is imported lazily inside `_deep_sleep_reset()` only, not at startup.

### Sensor Reliability
- I2C bus is initialised at 400 kHz (fast mode) explicitly, with a `board.I2C().deinit()` fallback if the bus is already claimed from a previous run.
- MLX90640 refresh rate is set to 1 Hz — matching the 15-second poll interval, reducing sensor self-heating artifacts and I2C traffic.
- Consecutive MemoryErrors on `getFrame()` are tolerated up to 10 times before triggering a reset, in case of transient errors.
- After 5 consecutive sensor read failures, the sensor is re-initialised in-place without a reboot.
- NaN values from the sensor are detected via the IEEE 754 identity `v != v` and replaced with the frame's minimum valid temperature before upload, preventing JSON parse errors at the API.

### Network Reliability
- WiFi radio is cycled (`enabled = False / True`) before every connection attempt, clearing stale ESP-IDF RF association state left by `SW_CPU_RESET`.
- DNS resolution is performed once after WiFi connects and the result is cached in `API_RESOLVED_IP`. If a DNS failure is reported by `_print_upload_oserror()`, the cache is cleared and re-resolved on the next attempt.
- `time.sleep(3)` after `sock.connect()` gives the lwIP TCP stack time to complete the 3-way handshake before the first `sock.send()`.
- Up to 3 upload attempts are made per cycle, with 1 second between attempts.
- After 5 consecutive upload failures, the WiFi radio is cycled to recover from the "associated but not routing" state that `wifi.radio.connected` cannot detect.
- `OSError` codes from failed uploads are decoded to human-readable messages (host unreachable, connection refused, timeout, DNS failure, EAGAIN) for easier serial-console diagnosis.
- `KeyboardInterrupt` (caused by DTR line toggles from serial terminal connect/disconnect on CH340/CP2102 boards) is caught at the outermost loop level and silently ignored, so the script survives plugging in a monitor.

### Watchdog and Recovery
- A software watchdog tracks the time since the last successful upload. If no upload succeeds for 900 seconds, `_deep_sleep_reset()` is called.
- `_deep_sleep_reset()` uses `alarm.exit_and_deep_sleep_until_alarms()` with a 5-second delay. Deep sleep fully powers down the RF subsystem and clears all IDF DRAM, ensuring a clean boot with full heap available. This is critical — `microcontroller.reset()` (SW_CPU_RESET) does not clear IDF DRAM and causes `espidf.MemoryError` at I2C init on the next boot.
- If the `alarm` module is unavailable, `microcontroller.reset()` is used as a fallback.

### Delta Gate
- Uploads are skipped when the scene's mean temperature has changed by less than `MIN_MEAN_DELTA_C` (0.2°C) since the last upload, reducing traffic in static rooms.
- A heartbeat forces an upload at least every `HEARTBEAT_INTERVAL_S` (60 seconds) regardless of the delta, ensuring fresh data reaches the API continuously.
- The heartbeat interval (60 s) is small enough that the dashboard never appears stale and the watchdog threshold (900 s) is safely far away.
