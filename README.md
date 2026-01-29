### ESP32 Devkit Setup

This is already done but I am leaving instructions here for future reference

We are using devkits equivalent to: https://circuitpython.org/board/doit_esp32_devkit_v1/

Flash Circuitpython firmware using Chrome or another browser supporting WebSerial, or using esptool.

https://circuitpython.org/board/doit_esp32_devkit_v1/

esptool steps:

Connect the devkit to USB, place it in the correct mode by holding down BOOT and pressing EN once.

Erase current flash with 'esptool erase_flash'

Upload Circuitpython firmware with 'esptool write_flash -z 0x0 adafruit-circuitpython-doit_esp32_devkit_v1-en_US-10.0.3.bin'

Test by connecting over serial with Putty/screen/tio

Network connection:

https://learn.adafruit.com/circuitpython-with-esp32-quick-start/setting-up-web-workflow

Use the REPL to store a settings.toml file with the SSID, Wifi password, and web API password OR use an editor that supports editing files over serial like Thonny.

Get the IP on the REPL with 

'import wifi

print("My IP address is", wifi.radio.ipv4_address)'

Point your web browser to that ip/code

### MLX90640 

Get the library bundle, install adafruit_mlx90640.mpy and adafruit_bus_device into the lib directory using the web workflow

https://circuitpython.org/libraries

https://learn.adafruit.com/adafruit-mlx90640-ir-thermal-camera/python-circuitpython

i2c scanner for testing

https://learn.adafruit.com/scanning-i2c-addresses/circuitpython

The api server script uses flask and requires flask and flask-cors to be installed in the virtualenv. 

Edit the mlx90640_uploader.py script with the IP or domain of the server to upload temperature values. Use a browser to access the server.

### Disable web workflow 
the web workflow can be disabled to save resouces by removing CIRCUITPY_WIFI_SSID and CIRCUITPY_WIFI_PASSWORD from settings.toml and using different variables os.getenv() to connect. This saves ~40KB of RAM

### Occupancy Estimation

The `occupancy_estimator.py` script analyzes thermal images to detect and count people in a room. It requires additional dependencies:

```bash
pip install -r requirements.txt
```

Run a single analysis:
```bash
python3 occupancy_estimator.py
```

Run continuous monitoring:
```bash
python3 occupancy_estimator.py --continuous
```

The script fetches thermal data from the API server and uses temperature thresholds and connected component analysis to detect warm bodies (people). Adjust the detection parameters at the top of the script:
- `MIN_HUMAN_TEMP` / `MAX_HUMAN_TEMP`: Temperature range for human detection
- `MIN_CLUSTER_SIZE` / `MAX_CLUSTER_SIZE`: Size constraints for person clusters
- `ROOM_TEMP_THRESHOLD`: Temperature difference from room temp to consider as person


