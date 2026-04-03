"""
ESP32 CircuitPython Thermal Camera Web Server
Reads thermal data from Adafruit MLX90640 sensor and displays on web page
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

# Initialize I2C bus - minimal memory usage
gc.collect()  # Free memory before I2C init
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
print("Initializing MLX90640 sensor...")
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

# WiFi configuration - read from settings.toml
# CIRCUITPY_WIFI_SSID = "your_ssid"
# CIRCUITPY_WIFI_PASSWORD = "your_password"

gc.collect()
ssid = os.getenv("CIRCUITPY_WIFI_SSID")
password = os.getenv("CIRCUITPY_WIFI_PASSWORD")

if not ssid or not password:
    raise ValueError("WiFi credentials not found")

wifi.radio.connect(ssid=ssid, password=password)
ip_addr = wifi.radio.ipv4_address
gc.collect()

pool = socketpool.SocketPool(wifi.radio)
gc.collect()

def temperature_to_color(temp, min_temp=20, max_temp=40):
    """
    Convert temperature to RGB color using a thermal color map.
    Returns (r, g, b) tuple with values 0-255.
    """
    # Clamp temperature to range
    temp = max(min_temp, min(max_temp, temp))
    
    # Handle edge case when all temperatures are the same
    if max_temp == min_temp:
        # Return a neutral gray color when temperature range is zero
        return (128, 128, 128)
    
    # Normalize to 0-1 range
    normalized = (temp - min_temp) / (max_temp - min_temp)
    
    # Thermal color mapping: blue -> cyan -> green -> yellow -> red
    if normalized < 0.25:
        # Blue to cyan
        r = 0
        g = int(normalized * 4 * 255)
        b = 255
    elif normalized < 0.5:
        # Cyan to green
        r = 0
        g = 255
        b = int((1 - (normalized - 0.25) * 4) * 255)
    elif normalized < 0.75:
        # Green to yellow
        r = int((normalized - 0.5) * 4 * 255)
        g = 255
        b = 0
    else:
        # Yellow to red
        r = 255
        g = int((1 - (normalized - 0.75) * 4) * 255)
        b = 0
    
    return (r, g, b)

def generate_thermal_image_json(frame_data, min_temp=None, max_temp=None):
    """
    Generate JSON representation of thermal image with color data.
    Returns JSON string with pixel colors and temperature values.
    Memory-optimized version for ESP32.
    """
    if min_temp is None:
        min_temp = min(frame_data)
    if max_temp is None:
        max_temp = max(frame_data)
    
    # Build JSON string directly to avoid large dictionary in memory
    # This is more memory-efficient than building a dict then converting
    json_parts = []
    json_parts.append('{"width":')
    json_parts.append(str(MLX_SHAPE[1]))
    json_parts.append(',"height":')
    json_parts.append(str(MLX_SHAPE[0]))
    json_parts.append(',"min_temp":')
    json_parts.append(str(round(min_temp, 2)))
    json_parts.append(',"max_temp":')
    json_parts.append(str(round(max_temp, 2)))
    json_parts.append(',"pixels":[')
    
    # Add pixels one at a time to reduce memory usage
    first_pixel = True
    for i, temp in enumerate(frame_data):
        if not first_pixel:
            json_parts.append(',')
        first_pixel = False
        
        row = i // MLX_SHAPE[1]
        col = i % MLX_SHAPE[1]
        r, g, b = temperature_to_color(temp, min_temp, max_temp)
        
        # Build pixel JSON directly
        json_parts.append('{"row":')
        json_parts.append(str(row))
        json_parts.append(',"col":')
        json_parts.append(str(col))
        json_parts.append(',"temp":')
        json_parts.append(str(round(temp, 2)))
        json_parts.append(',"r":')
        json_parts.append(str(r))
        json_parts.append(',"g":')
        json_parts.append(str(g))
        json_parts.append(',"b":')
        json_parts.append(str(b))
        json_parts.append('}')
    
    json_parts.append(']}')
    
    # Join all parts into final JSON string
    return ''.join(json_parts)

# Encoded once at startup — never changes, no need to rebuild or re-encode per request
HTML_PAGE = """<!DOCTYPE html>
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
    </style>
</head>
<body>
    <h1>ESP32 Thermal Camera</h1>
    <div class="info">
        <div>Min: <span id="minTemp">--</span>°C | Max: <span id="maxTemp">--</span>°C</div>
    </div>
    <canvas id="thermalCanvas" width="320" height="240"></canvas>

    <script>
        const canvas = document.getElementById('thermalCanvas');
        const ctx = canvas.getContext('2d');

        function refreshImage() {
            fetch('/thermal')
                .then(response => response.json())
                .then(data => {
                    drawThermalImage(data);
                    document.getElementById('minTemp').textContent = data.min_temp;
                    document.getElementById('maxTemp').textContent = data.max_temp;
                })
                .catch(error => {
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
</html>""".encode('utf-8')

# Allocated once at module level to avoid repeated heap churn on every request
_request_buffer = bytearray(2048)

def send_all_data(socket, data):
    """Send all data in chunks, handling EAGAIN errors by retrying."""
    try:
        if hasattr(socket, 'sendall'):
            socket.sendall(data)
            return len(data)
    except:
        pass
    
    total_sent = 0
    data_len = len(data)
    chunk_size = 128
    max_retries = 50
    retry_delay = 0.1
    
    while total_sent < data_len:
        remaining = data_len - total_sent
        current_chunk_size = min(chunk_size, remaining)
        chunk = data[total_sent:total_sent + current_chunk_size]
        
        retry_count = 0
        chunk_sent = 0
        
        while chunk_sent < current_chunk_size:
            try:
                sent = socket.send(chunk[chunk_sent:])
                if sent == 0:
                    raise OSError("Connection broken")
                chunk_sent += sent
                retry_count = 0
            except OSError as e:
                errno = getattr(e, 'errno', None)
                if errno == 11 or 'EAGAIN' in str(e):
                    retry_count += 1
                    if retry_count > max_retries:
                        raise
                    time.sleep(retry_delay)
                else:
                    raise
        
        total_sent += chunk_sent
        if total_sent < data_len:
            time.sleep(0.01)
        
    return total_sent

def handle_request(client_socket):
    """Handle HTTP requests."""
    try:
        # Minimal setup - reduce memory usage
        try:
            client_socket.setblocking(True)
        except:
            pass
        client_socket.settimeout(30.0)
        # CircuitPython sockets use recv_into() instead of recv()
        try:
            bytes_received = client_socket.recv_into(_request_buffer, 2048)
            print(f"  Received {bytes_received} bytes")
        except OSError as e:
            print(f"  Error receiving data: {e}")
            return

        if bytes_received == 0:
            print("  No data received, closing connection")
            return

        # Decode only the bytes that were actually received
        try:
            request = _request_buffer[:bytes_received].decode('utf-8')
        except UnicodeDecodeError as e:
            print(f"  Unicode decode error: {e}")
            # Try to decode what we can
            request = _request_buffer[:bytes_received].decode('utf-8', errors='ignore')
        
        print(f"  Request (first 200 chars): {request[:200]}")
        
        if not request or not request.strip():
            print("  Empty request, closing connection")
            return
        
        # Parse request - handle both \r\n and \n line endings
        request_lines = request.replace('\r\n', '\n').split('\n')
        if not request_lines:
            print("  No request lines found")
            return
            
        request_line = request_lines[0].strip()
        if not request_line:
            print("  Empty request line")
            return
            
        print(f"  Request line: {request_line}")
        parts = request_line.split(' ')
        if len(parts) < 2:
            print(f"  Invalid request line format: {parts}")
            return
        method = parts[0]
        path = parts[1].split('?')[0]  # Remove query parameters
        print(f"  Method: {method}, Path: {path}")
        
        if method == 'OPTIONS':
            response_headers = "HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            send_all_data(client_socket, response_headers.encode('utf-8'))
            return
        
        if path == '/' or path == '/index.html':
            response_headers = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(HTML_PAGE)}\r\nConnection: close\r\n\r\n"
            send_all_data(client_socket, response_headers.encode('utf-8') + HTML_PAGE)
            
        elif path == '/thermal':
            try:
                if mlx is None:
                    error_body = '{"error":"Sensor not initialized"}'
                    error_body_bytes = error_body.encode('utf-8')
                    response_headers = f"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(error_body_bytes)}\r\nConnection: close\r\n\r\n"
                    send_all_data(client_socket, response_headers.encode('utf-8') + error_body_bytes)
                else:
                    # Read frame with retry for memory issues
                    frame_read_success = False
                    for retry in range(3):
                        try:
                            gc.collect()
                            mlx.getFrame(frame)
                            frame_read_success = True
                            break
                        except MemoryError:
                            if retry < 2:
                                gc.collect()
                                time.sleep(0.1)
                            else:
                                error_body = '{"error":"Out of memory"}'
                                error_body_bytes = error_body.encode('utf-8')
                                response_headers = f"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(error_body_bytes)}\r\nConnection: close\r\n\r\n"
                                send_all_data(client_socket, response_headers.encode('utf-8') + error_body_bytes)
                                return
                        except Exception:
                            error_body = '{"error":"Sensor read failed"}'
                            error_body_bytes = error_body.encode('utf-8')
                            response_headers = f"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(error_body_bytes)}\r\nConnection: close\r\n\r\n"
                            send_all_data(client_socket, response_headers.encode('utf-8') + error_body_bytes)
                            return
                    
                    if frame_read_success:
                        gc.collect()
                        json_data = generate_thermal_image_json(frame)
                        json_bytes = json_data.encode('utf-8')
                        del json_data
                        gc.collect()
                        
                        response_headers = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(json_bytes)}\r\nConnection: close\r\n\r\n"
                        response = response_headers.encode('utf-8') + json_bytes
                        send_all_data(client_socket, response)
                        del response, json_bytes
                        gc.collect()
            except OSError as e:
                print(f"  ✗ Socket error in /thermal handler: {e}")
                try:
                    error_body = json.dumps({"error": "Socket error", "message": str(e)})
                    error_body_bytes = error_body.encode('utf-8')
                    response_headers = f"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(error_body_bytes)}\r\nConnection: close\r\n\r\n"
                    error_response = response_headers.encode('utf-8') + error_body_bytes
                    send_all_data(client_socket, error_response)
                except:
                    pass  # If we can't send error, just close
            except Exception as e:
                print(f"  ✗ Error in /thermal handler: {e}")
                try:
                    import sys
                    sys.print_exception(e)
                except:
                    pass
                try:
                    error_body = json.dumps({"error": "Server error", "message": str(e)})
                    error_body_bytes = error_body.encode('utf-8')
                    response_headers = f"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(error_body_bytes)}\r\nConnection: close\r\n\r\n"
                    error_response = response_headers.encode('utf-8') + error_body_bytes
                    send_all_data(client_socket, error_response)
                    print("  Sent error response")
                except Exception as e2:
                    print(f"  ✗ Failed to send error response: {e2}")
        elif path == '/favicon.ico':
            response_headers = "HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            send_all_data(client_socket, response_headers.encode('utf-8'))
        else:
            not_found_body = "404 Not Found"
            not_found_body_bytes = not_found_body.encode('utf-8')
            response_headers = f"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {len(not_found_body_bytes)}\r\nConnection: close\r\n\r\n"
            send_all_data(client_socket, response_headers.encode('utf-8') + not_found_body_bytes)
            
    except Exception:
        pass
    finally:
        try:
            client_socket.close()
        except:
            pass

# Start web server on port 8080
WEB_SERVER_PORT = 8080
gc.collect()
try:
    try:
        server_socket = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    except (AttributeError, TypeError):
        server_socket = pool.socket()
    
    try:
        server_socket.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
    except (AttributeError, TypeError):
        try:
            server_socket.setsockopt(1, 2, 1)
        except:
            pass
    
    server_socket.bind(('0.0.0.0', WEB_SERVER_PORT))
    server_socket.listen(5)
    gc.collect()
except Exception as e:
    raise

gc.collect()
print(f"Server: http://{ip_addr}:{WEB_SERVER_PORT}")

while True:
    try:
        client_socket, addr = server_socket.accept()
        handle_request(client_socket)
        gc.collect()
    except Exception:
        if not wifi.radio.connected:
            print("WiFi disconnected, reconnecting...")
            try:
                wifi.radio.connect(ssid=ssid, password=password)
                ip_addr = wifi.radio.ipv4_address
                print(f"Reconnected: {ip_addr}")
                # Rebuild the server socket on the new network connection
                try:
                    server_socket.close()
                except:
                    pass
                pool = socketpool.SocketPool(wifi.radio)
                try:
                    server_socket = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
                except (AttributeError, TypeError):
                    server_socket = pool.socket()
                try:
                    server_socket.setsockopt(pool.SOL_SOCKET, pool.SO_REUSEADDR, 1)
                except (AttributeError, TypeError):
                    try:
                        server_socket.setsockopt(1, 2, 1)
                    except:
                        pass
                server_socket.bind(('0.0.0.0', WEB_SERVER_PORT))
                server_socket.listen(5)
                print(f"Server restarted: http://{ip_addr}:{WEB_SERVER_PORT}")
            except Exception as e:
                print(f"WiFi reconnect failed: {e}")
                time.sleep(5.0)
        else:
            time.sleep(0.1)


