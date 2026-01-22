# Thermal Camera API Setup

This setup uses an API server on your laptop to receive thermal data from the ESP32, instead of running a web server on the ESP32 itself.

## Setup Instructions

### 1. Install API Server Dependencies

On your laptop, install the required Python packages:

```bash
pip install flask flask-cors
```

Or use the requirements file:

```bash
pip install -r requirements.txt
```

### 2. Find Your Laptop's IP Address

**Linux/Mac:**
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
# or
hostname -I
```

**Windows:**
```bash
ipconfig
# Look for IPv4 Address (not 127.0.0.1)
```

### 3. Configure ESP32 Script

Edit `mlx90640_uploader.py` and update the API_URL:

```python
API_URL = "http://YOUR_LAPTOP_IP:5000/api/thermal"
```

Replace `YOUR_LAPTOP_IP` with your actual IP address (e.g., `10.0.0.111`).

### 4. Start the API Server

On your laptop, run:

```bash
python3 api_server.py
```

The server will start on port 5000 and display:
- API endpoint: `http://localhost:5000/api/thermal`
- Web interface: `http://localhost:5000`

### 5. Upload ESP32 Script

Upload `mlx90640_uploader.py` to your ESP32 as `code.py` (or run it from REPL with `exec()`).

### 6. View Thermal Data

Open your web browser and go to:
```
http://localhost:5000
```

The page will automatically refresh every second to show the latest thermal data from the ESP32.

## How It Works

1. **ESP32** (`mlx90640_uploader.py`):
   - Reads thermal data from MLX90640 sensor every 3 seconds
   - Generates JSON with temperature and color data
   - Uploads data to API server via HTTP POST

2. **API Server** (`api_server.py`):
   - Receives thermal data via POST requests
   - Stores the latest data
   - Serves web interface to view the data
   - Provides GET endpoint to retrieve latest data

## Benefits

- **Less memory usage on ESP32** - No (additional) web server running on the device
- **Better performance** - Laptop handles web serving
- **Easier debugging** - Can see API requests on laptop
- **More flexible** - Can add features like data logging, history, etc.

## Troubleshooting

- **ESP32 can't connect**: Check firewall settings on your laptop
- **No data appearing**: Verify the API_URL in the ESP32 script matches your laptop's IP
- **Connection timeout**: Make sure both devices are on the same network
