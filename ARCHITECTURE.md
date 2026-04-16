# ESP32 Thermal Camera System - Architecture Overview

## Repository Structure

This repository contains a complete thermal imaging and occupancy detection system using an ESP32 microcontroller with an MLX90640 thermal sensor. The system uses a client-server architecture to offload processing from the resource-constrained ESP32 to a more capable laptop/server.

## File Organization

### Core Application Files

#### **ESP32 Scripts (CircuitPython)**

1. **`mlx90640_uploader.py`** (Primary ESP32 Script)
   - **Purpose**: Lightweight data collection and upload script
   - **Functionality**:
     - Reads thermal data from MLX90640 sensor via I2C
     - Generates compact JSON format (raw temperatures only)
     - Uploads data to laptop API server via HTTP POST
     - Configurable upload interval (default: 15 seconds)
     - Memory-optimized for ESP32 constraints
   - **Key Features**:
     - Minimal memory footprint (no web server, no color mapping)
     - Automatic garbage collection
     - Error handling for network and sensor issues
     - WiFi configuration via `settings.toml`

2. **`mlx90640.py`** (Legacy Web Server Script)
   - **Purpose**: Original implementation with embedded web server
   - **Functionality**:
     - Hosts web server directly on ESP32 (port 8080)
     - Serves thermal images with color mapping
     - Handles HTTP requests manually using CircuitPython sockets
     - **Note**: Replaced by client-server architecture for better performance

3. **`test_mlx90640.py`** (Testing/Debugging Script)
   - **Purpose**: Standalone sensor testing utility
   - **Functionality**:
     - Tests I2C communication
     - Validates sensor initialization
     - Reads and displays raw temperature data
     - Helps diagnose hardware/connection issues
   - **Usage**: Run independently to verify sensor functionality

#### **Server-Side Scripts (Python 3)**

4. **`api_server.py`** (Main API Server)
   - **Purpose**: Flask-based API server running on laptop
   - **Functionality**:
     - Receives thermal data from ESP32 via POST requests
     - Performs occupancy estimation using computer vision
     - Serves web interface for real-time visualization
     - Saves all thermal and occupancy data to disk
     - Provides REST API endpoints for data access
   - **Key Components**:
     - **Data Reception**: `/api/thermal` (POST) - Receives compact thermal data
     - **Data Retrieval**: `/api/thermal` (GET) - Returns latest thermal data with occupancy
     - **Occupancy History**: `/api/occupancy/history` - Historical occupancy data
     - **Occupancy Stats**: `/api/occupancy/stats` - Statistical analysis
     - **Web Interface**: `/` - Real-time thermal visualization with occupancy graph
   - **Features**:
     - Automatic occupancy detection (people counting)
     - Data persistence (JSON files)
     - Real-time web dashboard with Chart.js graphs
     - Temperature-to-color mapping (server-side)
     - Room temperature estimation

5. **`occupancy_estimator.py`** (Standalone Analysis Tool)
   - **Purpose**: Independent occupancy analysis script
   - **Functionality**:
     - Fetches thermal data from API server
     - Performs occupancy estimation
     - Can run single analysis or continuous monitoring
     - Provides detailed reports
   - **Use Cases**: 
     - Offline analysis of saved data
     - Testing occupancy detection algorithms
     - Command-line monitoring

### Configuration Files

6. **`settings.toml.example`**
   - Template for CircuitPython WiFi configuration
   - Contains placeholders for SSID and password
   - Must be copied to `settings.toml` on CIRCUITPY drive

7. **`requirements.txt`**
   - Python dependencies for server-side scripts
   - Includes: Flask, Flask-CORS, NumPy, SciPy, Requests

### Documentation Files

8. **`README.md`**
   - Main project documentation
   - Setup instructions
   - Usage guidelines
   - Hardware requirements

9. **`API_SETUP.md`**
   - Detailed API server setup guide
   - Network configuration instructions
   - Troubleshooting tips

## System Architecture

### Data Flow

```
┌─────────────────┐
│   MLX90640      │
│  Thermal Sensor │
└────────┬────────┘
         │ I2C
         ▼
┌─────────────────┐
│     ESP32       │
│  (CircuitPython)│
│                 │
│ mlx90640_       │
│ uploader.py     │
└────────┬────────┘
         │ HTTP POST
         │ (Compact JSON)
         ▼
┌─────────────────┐
│  Laptop/Server  │
│                 │
│  api_server.py  │
│  (Flask)        │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐ ┌──────────────┐
│  Web   │ │  Data Files  │
│ Browser│ │ (thermal_data│
│        │ │  /)          │
└────────┘ └──────────────┘
```

### Component Responsibilities

#### **ESP32 (Edge Device)**
- **Hardware Interface**: I2C communication with MLX90640
- **Data Collection**: Reads 24x32 pixel thermal frames (768 temperatures)
- **Data Format**: Compact JSON with raw temperature values
- **Network**: WiFi connection, HTTP client
- **Constraints**: Limited RAM (~520KB), minimal processing

#### **Laptop/Server (Processing Hub)**
- **Data Reception**: Flask API endpoint for POST requests
- **Processing**: 
  - Temperature-to-color mapping
  - Occupancy estimation (computer vision)
  - Room temperature calculation
- **Storage**: File-based persistence (JSON/JSONL)
- **Visualization**: Web interface with real-time updates
- **API**: RESTful endpoints for data access

## Key Algorithms

### 1. Occupancy Detection

**Location**: `api_server.py` → `estimate_occupancy()`

**Process**:
1. Convert thermal data to 2D numpy array (24x32 grid)
2. Estimate room temperature (median of all pixels)
3. Create binary mask of human body heat:
   - Absolute threshold: 30-45°C (human body temperature range)
   - Relative threshold: Pixels warmer than room by 0.5°C
   - Combine both conditions (AND operation)
4. Find connected components (8-connected clustering)
5. Filter clusters by size (3-200 pixels)
6. Count valid clusters as people

**Parameters** (configurable):
- `MIN_HUMAN_TEMP`: 30.0°C
- `MAX_HUMAN_TEMP`: 45.0°C
- `MIN_CLUSTER_SIZE`: 3 pixels
- `MAX_CLUSTER_SIZE`: 200 pixels
- `ROOM_TEMP_THRESHOLD`: 0.5°C

### 2. Temperature-to-Color Mapping

**Location**: `api_server.py` → `temperature_to_color()`

**Algorithm**:
- Normalize temperature to 0-1 range (min to max)
- Color gradient:
  - 0.0-0.25: Blue to Cyan (cold)
  - 0.25-0.5: Cyan to Green
  - 0.5-0.75: Green to Yellow
  - 0.75-1.0: Yellow to Red (hot)

### 3. Data Formats

**Compact Format** (ESP32 → Server):
```json
{
  "sensor_id": "room-101",
  "w": 32,
  "h": 24,
  "min": 20.5,
  "max": 35.2,
  "t": [20.5, 20.6, ...]
}
```

**Expanded Format** (Server → Web):
```json
{
  "width": 32,
  "height": 24,
  "min_temp": 20.5,
  "max_temp": 35.2,
  "pixels": [
    {"row": 0, "col": 0, "temp": 20.5, "r": 0, "g": 128, "b": 255},
    ...
  ]
}
```

## Data Storage

### Directory Structure
```
thermal_data/
├── thermal_{sensor_id}_YYYYMMDD_HHMMSS_MMM_compact.json   # Original ESP32 data
├── thermal_{sensor_id}_YYYYMMDD_HHMMSS_MMM_expanded.json  # Processed with colors
└── occupancy_YYYYMMDD.jsonl                               # Daily log, all sensors
```

### File Formats

1. **Thermal Data Files** (JSON):
   - Timestamped entries
   - Both compact and expanded formats saved
   - Includes full pixel data with colors

2. **Occupancy Log** (JSONL - JSON Lines):
   - One JSON object per line
   - Daily files (one per day)
   - Contains: timestamp, occupancy count, room temperature, cluster data

## Web Interface Features

### Real-Time Dashboard
- **Thermal Image**: Color-coded 24x32 pixel visualization
- **Temperature Stats**: Min/Max temperatures displayed
- **Occupancy Display**: Current person count
- **Room Temperature**: Estimated background temperature
- **Occupancy Graph**: Chart.js line chart showing occupancy over time
- **Auto-Refresh**: Thermal image updates every 1 second, graph every 5 seconds

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/thermal` | POST | Receive thermal data from ESP32 |
| `/api/thermal` | GET | Get latest thermal data with occupancy |
| `/api/occupancy/history` | GET | Get historical occupancy data (date parameter) |
| `/api/occupancy/stats` | GET | Get occupancy statistics (date parameter) |
| `/api/test` | GET | Server health check |
| `/` | GET | Web interface (HTML page) |

## Memory Optimization Strategies

### ESP32 Constraints
- **Total RAM**: ~520KB
- **Challenge**: Web server + sensor + network stack = memory overflow
- **Solution**: Client-server architecture

### Optimizations Applied

1. **Compact Data Format**: Send only raw temperatures (no colors)
2. **No Web Server on ESP32**: Offload to laptop
3. **Aggressive Garbage Collection**: `gc.collect()` after major operations
4. **Minimal Print Statements**: Reduce string literal memory
5. **Direct JSON String Building**: Avoid large intermediate dictionaries
6. **Chunked Socket Sends**: Handle `EAGAIN` errors gracefully

## Configuration

### ESP32 Configuration
- **WiFi**: Set in `settings.toml` on CIRCUITPY drive
- **API URL**: Set in `mlx90640_uploader.py` → `API_URL`
- **Upload Interval**: Set in `mlx90640_uploader.py` → `UPLOAD_INTERVAL`

### Server Configuration
- **Data Directory**: `api_server.py` → `DATA_DIR`
- **Data Saving**: `api_server.py` → `SAVE_DATA` (True/False)
- **Occupancy Parameters**: `api_server.py` → Detection thresholds
- **Port**: Default 5000 (Flask default)

## Dependencies

### CircuitPython Libraries (ESP32)
- `adafruit_mlx90640` - Thermal sensor driver
- `adafruit_bus_device` - I2C bus support
- Built-in: `wifi`, `socketpool`, `board`, `busio`

### Python Libraries (Server)
- `flask` - Web framework
- `flask-cors` - Cross-origin resource sharing
- `numpy` - Array operations
- `scipy` - Scientific computing (connected components)
- `requests` - HTTP client (for occupancy_estimator.py)

## Development History

The project evolved through several iterations:

1. **Initial**: Embedded web server on ESP32 (`mlx90640.py`)
   - Memory issues with full web server
   - Port conflicts with CircuitPython web workflow

2. **Optimized**: Memory optimizations applied
   - Reduced print statements
   - Optimized JSON generation
   - Aggressive garbage collection

3. **Current**: Client-server architecture
   - ESP32 as data collector only
   - Server handles processing and visualization
   - Better performance and extensibility

## Usage Workflow

1. **Setup**:
   - Flash CircuitPython to ESP32
   - Install libraries via web workflow
   - Configure WiFi in `settings.toml`

2. **Start Server**:
   ```bash
   python3 api_server.py
   ```

3. **Upload ESP32 Script**:
   - Copy `mlx90640_uploader.py` to ESP32 as `code.py`
   - Update `API_URL` with laptop's IP address

4. **View Data**:
   - Open browser to `http://localhost:5000`
   - Real-time thermal image and occupancy display

5. **Analyze Data** (Optional):
   ```bash
   python3 occupancy_estimator.py --continuous
   ```

## Future Enhancements

Potential improvements:
- Database storage (SQLite/PostgreSQL) instead of files
- Multi-room support with multiple ESP32s
- Machine learning for better occupancy detection
- Mobile app interface
- Historical data analysis dashboard
- Alert system for occupancy thresholds
- Integration with home automation systems
