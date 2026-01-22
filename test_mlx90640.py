"""
Test script for MLX90640 thermal sensor
Simple test to verify the sensor is working correctly
"""

# Immediate test to verify script is running
print("TEST SCRIPT STARTING")
print("=" * 50)
print("MLX90640 Sensor Test")
print("=" * 50)

import time
print("time module imported")

import board
print("board module imported")

import busio
print("busio module imported")

# Thermal image dimensions
MLX_SHAPE = (24, 32)  # 24 rows, 32 columns
FRAME_SIZE = MLX_SHAPE[0] * MLX_SHAPE[1]  # 768 pixels

print(f"Frame size: {FRAME_SIZE} pixels")

def main():
    """Main test function."""
    print("\n=== Starting MLX90640 Test ===\n")
    
    # Initialize I2C bus
    print("1. Initializing I2C bus...")
    i2c = None
    try:
        print(f"   SCL pin: {board.SCL}")
        print(f"   SDA pin: {board.SDA}")
        
        # Try to create I2C bus
        print("   Creating I2C bus...")
        try:
            i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
            print("   ✓ I2C bus initialized")
        except ValueError as e:
            error_msg = str(e).lower()
            if "in use" in error_msg:
                print("   ⚠ I2C pins are already in use!")
                print("   This usually means another script is using I2C (like mlx90640.py)")
                print("\n   Solutions:")
                print("   1. Stop the web server (rename mlx90640.py or delete code.py)")
                print("   2. Or run this test from REPL after stopping the server")
                print("   3. Or use board.I2C() if available (trying now)...")
                
                # Try to use board.I2C() if available
                try:
                    if hasattr(board, 'I2C'):
                        i2c = board.I2C()
                        print("   ✓ Using board.I2C() instead")
                    else:
                        raise ValueError("board.I2C() not available")
                except:
                    print("\n   ✗ Cannot initialize I2C. Please stop other I2C-using code first.")
                    print("   To stop the web server:")
                    print("     - Delete or rename code.py on CIRCUITPY drive")
                    print("     - Or press Ctrl+C in REPL if server is running")
                    raise ValueError("I2C pins in use - stop other code first")
            else:
                raise
        
        # Scan I2C bus for devices
        print("\n   Scanning I2C bus for devices...")
        while not i2c.try_lock():
            pass
        try:
            devices = i2c.scan()
            print(f"   Found {len(devices)} I2C device(s):")
            for device in devices:
                print(f"     - Address: 0x{device:02X} (decimal {device})")
            if len(devices) == 0:
                print("   ⚠ WARNING: No I2C devices found!")
            # MLX90640 typically uses address 0x33
            if 0x33 in devices:
                print("   ✓ MLX90640 detected at address 0x33")
            else:
                print("   ⚠ MLX90640 not found at expected address 0x33")
        finally:
            i2c.unlock()
    except Exception as e:
        print(f"   ✗ Error initializing I2C: {e}")
        import sys
        try:
            sys.print_exception(e)
        except:
            pass
        return
    
    # Initialize MLX90640 sensor
    print("\n2. Importing MLX90640 library...")
    try:
        import adafruit_mlx90640
        from adafruit_mlx90640 import RefreshRate
        print("   ✓ Library imported successfully")
    except Exception as e:
        print(f"   ✗ Error importing library: {e}")
        import sys
        try:
            sys.print_exception(e)
        except:
            pass
        return
    
    print("\n3. Initializing MLX90640 sensor...")
    try:
        print("   Creating MLX90640 object...")
        # Try different I2C addresses if default fails
        mlx = None
        addresses_to_try = [0x33]  # Default MLX90640 address
        
        # If default address not found, try scanning
        while not i2c.try_lock():
            pass
        try:
            devices = i2c.scan()
            if 0x33 not in devices and len(devices) > 0:
                print(f"   Default address 0x33 not found, trying detected addresses...")
                addresses_to_try = devices
        finally:
            i2c.unlock()
        
        for addr in addresses_to_try:
            try:
                print(f"   Trying address 0x{addr:02X}...")
                # Note: adafruit_mlx90640 doesn't support address parameter directly
                # But we can check if device responds
                mlx = adafruit_mlx90640.MLX90640(i2c)
                print(f"   ✓ MLX90640 object created")
                break
            except Exception as e:
                print(f"   ✗ Failed at address 0x{addr:02X}: {e}")
                mlx = None
        
        if mlx is None:
            raise Exception("Could not initialize MLX90640 at any address")
        
        print("   Setting refresh rate...")
        # Try different refresh rates if needed
        try:
            mlx.refresh_rate = RefreshRate.REFRESH_8_HZ  # 8Hz refresh rate
            print(f"   ✓ Refresh rate set to: {mlx.refresh_rate}")
        except Exception as e:
            print(f"   ⚠ Could not set refresh rate: {e}")
            print("   Continuing with default refresh rate...")
        
        print("   ✓ MLX90640 sensor initialized successfully")
        
        # Try to read sensor serial number or other info if available
        try:
            # Some MLX90640 libraries expose serial number
            if hasattr(mlx, 'serial_number'):
                print(f"   Serial number: {mlx.serial_number}")
        except:
            pass
            
    except Exception as e:
        print(f"   ✗ Error initializing sensor: {e}")
        import sys
        try:
            sys.print_exception(e)
        except:
            pass
        return
    
    # Frame buffer for thermal data
    frame = [0] * FRAME_SIZE
    
    print("\n4. Testing sensor read...")
    print("   Reading thermal frames (press Ctrl+C to stop)\n")
    
    frame_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    try:
        while True:
            try:
                # Read a frame from the sensor
                print(f"   Attempting to read frame #{frame_count + 1}...", end=" ")
                mlx.getFrame(frame)
                print("✓")
                frame_count += 1
                consecutive_errors = 0  # Reset error counter on success
                
                # Calculate statistics
                min_temp = min(frame)
                max_temp = max(frame)
                avg_temp = sum(frame) / len(frame)
                
                # Find center temperature
                center_idx = (MLX_SHAPE[0] // 2) * MLX_SHAPE[1] + (MLX_SHAPE[1] // 2)
                center_temp = frame[center_idx]
                
                # Display results
                print(f"Frame #{frame_count}:")
                print(f"  Min: {min_temp:.2f}°C")
                print(f"  Max: {max_temp:.2f}°C")
                print(f"  Avg: {avg_temp:.2f}°C")
                print(f"  Center: {center_temp:.2f}°C")
                
                # Display a small sample of the frame (4x4 center area)
                print("  Sample (4x4 center area):")
                start_row = MLX_SHAPE[0] // 2 - 2
                start_col = MLX_SHAPE[1] // 2 - 2
                for row in range(4):
                    row_data = []
                    for col in range(4):
                        idx = (start_row + row) * MLX_SHAPE[1] + (start_col + col)
                        row_data.append(f"{frame[idx]:5.1f}")
                    print(f"    {' '.join(row_data)}")
                
                print()
                time.sleep(1)  # Wait 1 second between frames
                
            except Exception as e:
                consecutive_errors += 1
                print(f"\n   ✗ Error reading frame #{frame_count + 1}: {e}")
                import sys
                try:
                    sys.print_exception(e)
                except:
                    pass
                
                if consecutive_errors >= max_consecutive_errors:
                    print(f"\n   ⚠ {max_consecutive_errors} consecutive errors. Sensor may not be responding.")
                    print("   Check:")
                    print("     - I2C connections (SDA, SCL)")
                    print("     - Power supply (3.3V)")
                    print("     - Sensor address (should be 0x33)")
                    print("     - Pull-up resistors on I2C lines")
                    print("   Continuing to retry...\n")
                    consecutive_errors = 0  # Reset to keep trying
                
                time.sleep(1)
                
    except KeyboardInterrupt:
        print("\n\nTest stopped by user")
        print(f"Total frames read: {frame_count}")
        print("=" * 50)

# In CircuitPython, always call main() when the module is loaded
# This ensures it runs whether imported or executed directly
# Note: When using exec(), this will run. When importing, it should also run.
print("Module loaded, calling main()...")
try:
    main()
except Exception as e:
    print(f"Error calling main(): {e}")
    import sys
    try:
        sys.print_exception(e)
    except:
        pass
