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

Use the REPL to store a settings.toml file with the SSID, Wifi password, and web API password.

Get the IP on the REPL with 

'import wifi

print("My IP address is", wifi.radio.ipv4_address)'

Point your web browser to that ip/code
