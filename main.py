import asyncio
import logging
import sys
import struct
import time

from bleak import BleakServer, BleakGATTCharacteristic, BleakError

# --- Configuration ---
# Official FTMS UUIDs
FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
CONTROL_POINT_CHAR_UUID = "00002AD9-0000-1000-8000-00805f9b34fb"
STATUS_CHAR_UUID = "00002ADA-0000-1000-8000-00805f9b34fb"
FEATURE_CHAR_UUID = "00002ACC-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_CHAR_UUID = "00002AD2-0000-1000-8000-00805f9b34fb"
TRAINING_STATUS_CHAR_UUID = "00002AD3-0000-1000-8000-00805f9b34fb"
SUPPORTED_RESISTANCE_LEVEL_RANGE_CHAR_UUID = "00002AD6-0000-1000-8000-00805f9b34fb"
SUPPORTED_POWER_RANGE_CHAR_UUID = "00002AD8-0000-1000-8000-00805f9b34fb"

DEVICE_NAME = "DIY FTMS Bike"

# FTMS Control Point Response Codes
RESPONSE_SUCCESS = 0x01
RESPONSE_NOT_SUPPORTED = 0x02
RESPONSE_INVALID_PARAMETER = 0x03
RESPONSE_OPERATION_FAILED = 0x04
RESPONSE_CONTROL_NOT_PERMITTED = 0x05

# FTMS Control Point Op Codes
CP_REQUEST_CONTROL = 0x00
CP_RESET = 0x01
CP_SET_TARGET_SPEED = 0x02
CP_SET_TARGET_INCLINATION = 0x03
CP_SET_TARGET_RESISTANCE = 0x04
CP_SET_TARGET_POWER = 0x05
CP_START_OR_RESUME = 0x07
CP_STOP_OR_PAUSE = 0x08

# Feature flags (16 bytes/128 bits)
FTMS_FEATURES = bytearray([
    0x54, 0x08, 0x00, 0x00,  # Fitness Machine Features (bits 0-31)
    # - Inclination supported [bit 6]
    # - Resistance level supported [bit 2]
    # - Power measurement supported [bit 14]
    # - Heart rate measurement supported [bit 16]
    0xA2, 0x00, 0x00, 0x00,  # Target Setting Features (bits 32-63)
    # - Resistance level target setting supported [bit 1]
    # - Power target setting supported [bit 7]
    0x00, 0x00, 0x00, 0x00,  # bits 64-95
    0x00, 0x00, 0x00, 0x00   # bits 96-127
])

# Supported ranges
RESISTANCE_RANGE = bytearray(struct.pack("<hhh", 0, 3000, 10))  # Min: 0, Max: 3000, Increment: 10 (0.1 units)
POWER_RANGE = bytearray(struct.pack("<hhh", 0, 2000, 1))  # Min: 0W, Max: 2000W, Increment: 1W

# Training status values
STATUS_IDLE = 0x01
STATUS_ACTIVE = 0x02
STATUS_PAUSED = 0x03
STATUS_COMPLETED = 0x04

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global state
current_resistance = 0  # In 0.1 units (0-3000)
current_power = 100     # In watts (0-2000)
current_speed = 1000    # In 0.01 km/h units (10 km/h)
current_cadence = 140   # In 0.5 RPM units (70 RPM)
current_hr = 0          # Heart rate, if available
training_status = STATUS_IDLE
has_control = False     # Whether client has control
client_writes_enabled = True  # Whether client writes are permitted

# Control point response queue
control_point_response = None
control_point_char = None  # Will be populated when characteristic is created

async def send_control_point_response(request_op_code, response_code, values=None):
    """Sends a response to a control point request via indication."""
    global control_point_char
    
    if control_point_char is None:
        logger.error("Control point characteristic not initialized")
        return
    
    # Format is: Response Code (0x80) + Request Op Code + Result Code + [Values]
    response = bytearray([0x80, request_op_code, response_code])
    if values:
        response.extend(values)
    
    logger.info(f"Sending control point response: {response.hex()}")
    await control_point_char.service.server.indicate(control_point_char, response)

def generate_indoor_bike_data():
    """Generates a simulated indoor bike data packet according to FTMS spec."""
    # Flags: Speed present, Cadence present, Power present, HR present
    flags = 0b00001111
    
    # Format according to FTMS spec:
    # [Flags (2 bytes)][Speed (2 bytes)][Cadence (2 bytes)][Power (2 bytes)][HR (1 byte)]
    return struct.pack("<HHHHb", 
                      flags,
                      current_speed,  # Speed in 0.01 km/h units
                      current_cadence,  # Cadence in 0.5 RPM units
                      current_power,  # Power in watts
                      current_hr if current_hr else 0)

async def update_bike_data(indoor_bike_data_char):
    """Updates the indoor bike data characteristic with simulated values."""
    if not indoor_bike_data_char:
        return
        
    try:
        # Simulate changes in resistance affecting power and speed
        # In a real implementation, this would come from real sensors
        global current_power, current_speed
        
        # Generate data packet and notify clients
        data = generate_indoor_bike_data()
        await indoor_bike_data_char.service.server.notify(indoor_bike_data_char, data)
        logger.debug(f"Updated bike data: speed={current_speed/100}km/h, power={current_power}W, cadence={current_cadence/2}RPM")
    except Exception as e:
        logger.error(f"Error updating bike data: {e}")

# --- Control Point Handler ---
async def handle_control_point_write(characteristic: BleakGATTCharacteristic, data: bytearray):
    """Handles writes to the control point characteristic."""
    global has_control, current_resistance, current_power, training_status
    
    logger.info(f"Received control point command: {data.hex()}")
    
    if not data:
        logger.warning("Received empty data on control point.")
        return

    op_code = data[0]
    logger.info(f"  Op Code: {op_code}")

    # Handle specific opcodes as defined in FTMS specification
    try:
        if op_code == CP_REQUEST_CONTROL:
            has_control = True
            logger.info("Client requested control - granted")
            await send_control_point_response(CP_REQUEST_CONTROL, RESPONSE_SUCCESS)
            
        elif op_code == CP_RESET:
            current_resistance = 0
            current_power = 100
            logger.info("Received reset command")
            await send_control_point_response(CP_RESET, RESPONSE_SUCCESS)
            
        elif op_code == CP_SET_TARGET_RESISTANCE:
            if not has_control:
                logger.warning("Client attempted to set resistance without control")
                await send_control_point_response(CP_SET_TARGET_RESISTANCE, RESPONSE_CONTROL_NOT_PERMITTED)
                return
                
            if len(data) < 3:  # Opcode + 2 bytes for sint16
                logger.warning("Invalid resistance data format")
                await send_control_point_response(CP_SET_TARGET_RESISTANCE, RESPONSE_INVALID_PARAMETER)
                return
                
            # Parse resistance value (signed 16-bit integer, little-endian)
            resistance = struct.unpack("<h", data[1:3])[0]
            logger.info(f"Setting resistance level to {resistance/10.0}%")
            
            if resistance < 0 or resistance > 3000:  # Check against our defined range
                logger.warning(f"Resistance value {resistance} out of range")
                await send_control_point_response(CP_SET_TARGET_RESISTANCE, RESPONSE_INVALID_PARAMETER)
                return
                
            current_resistance = resistance
            # Here you would add code to physically change resistance
            # Update power and speed based on resistance change (simplified model)
            current_power = max(50, min(2000, int(100 + (resistance / 10))))
            
            await send_control_point_response(CP_SET_TARGET_RESISTANCE, RESPONSE_SUCCESS)
            
        elif op_code == CP_SET_TARGET_POWER:
            if not has_control:
                logger.warning("Client attempted to set power without control")
                await send_control_point_response(CP_SET_TARGET_POWER, RESPONSE_CONTROL_NOT_PERMITTED)
                return
                
            if len(data) < 3:  # Opcode + 2 bytes for sint16
                logger.warning("Invalid power data format")
                await send_control_point_response(CP_SET_TARGET_POWER, RESPONSE_INVALID_PARAMETER)
                return
                
            # Parse power value (signed 16-bit integer, little-endian)
            power = struct.unpack("<h", data[1:3])[0]
            logger.info(f"Setting target power to {power}W")
            
            if power < 0 or power > 2000:  # Check against our defined range
                logger.warning(f"Power value {power} out of range")
                await send_control_point_response(CP_SET_TARGET_POWER, RESPONSE_INVALID_PARAMETER)
                return
                
            current_power = power
            # Here you would add code to physically adjust to maintain target power
            
            await send_control_point_response(CP_SET_TARGET_POWER, RESPONSE_SUCCESS)
            
        elif op_code == CP_START_OR_RESUME:
            logger.info("Starting/Resuming training session")
            training_status = STATUS_ACTIVE
            await send_control_point_response(CP_START_OR_RESUME, RESPONSE_SUCCESS)
            
        elif op_code == CP_STOP_OR_PAUSE:
            if len(data) < 2:
                logger.warning("Invalid stop/pause data format")
                await send_control_point_response(CP_STOP_OR_PAUSE, RESPONSE_INVALID_PARAMETER)
                return
                
            stop_or_pause = data[1]
            if stop_or_pause == 0x01:  # Stop
                logger.info("Stopping training session")
                training_status = STATUS_IDLE
            elif stop_or_pause == 0x02:  # Pause
                logger.info("Pausing training session")
                training_status = STATUS_PAUSED
            else:
                logger.warning(f"Invalid stop/pause parameter: {stop_or_pause}")
                await send_control_point_response(CP_STOP_OR_PAUSE, RESPONSE_INVALID_PARAMETER)
                return
                
            await send_control_point_response(CP_STOP_OR_PAUSE, RESPONSE_SUCCESS)
            
        else:
            logger.warning(f"Unsupported op code: {op_code}")
            await send_control_point_response(op_code, RESPONSE_NOT_SUPPORTED)
            
    except Exception as e:
        logger.error(f"Error handling control point command: {e}", exc_info=True)
        try:
            await send_control_point_response(op_code, RESPONSE_OPERATION_FAILED)
        except:
            logger.error("Failed to send error response")

# --- Main Server Logic ---
async def run_server():
    """Runs the BLE GATT server."""
    global control_point_char
    
    logger.info(f"Starting GATT server as '{DEVICE_NAME}'")
    logger.info(f"Service UUID: {FTMS_SERVICE_UUID}")

    try:
        async with BleakServer(DEVICE_NAME) as server:
            logger.info("Server started. Advertising...")

            # Add the FTMS service
            await server.add_service(uuid=FTMS_SERVICE_UUID, is_primary=True)
            
            # Add the FTMS Feature characteristic (mandatory, read-only)
            feature_char = await server.add_characteristic(
                uuid=FEATURE_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.READ,
                value=FTMS_FEATURES,
            )
            logger.info(f"Added Feature characteristic: {FEATURE_CHAR_UUID}")
            
            # Add Supported Resistance Level Range characteristic (read-only)
            resistance_range_char = await server.add_characteristic(
                uuid=SUPPORTED_RESISTANCE_LEVEL_RANGE_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.READ,
                value=RESISTANCE_RANGE,
            )
            logger.info(f"Added Resistance Range characteristic: {SUPPORTED_RESISTANCE_LEVEL_RANGE_CHAR_UUID}")
            
            # Add Supported Power Range characteristic (read-only)
            power_range_char = await server.add_characteristic(
                uuid=SUPPORTED_POWER_RANGE_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.READ,
                value=POWER_RANGE,
            )
            logger.info(f"Added Power Range characteristic: {SUPPORTED_POWER_RANGE_CHAR_UUID}")
            
            # Add Indoor Bike Data characteristic (notify-only)
            indoor_bike_data_char = await server.add_characteristic(
                uuid=INDOOR_BIKE_DATA_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.NOTIFY,
            )
            logger.info(f"Added Indoor Bike Data characteristic: {INDOOR_BIKE_DATA_CHAR_UUID}")
            
            # Add Training Status characteristic (read, notify)
            training_status_char = await server.add_characteristic(
                uuid=TRAINING_STATUS_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.READ | BleakGATTCharacteristic.NOTIFY,
                value=bytearray([training_status]),  # Initial value
            )
            logger.info(f"Added Training Status characteristic: {TRAINING_STATUS_CHAR_UUID}")
            
            # Add Control Point characteristic (write, indicate)
            control_point_char = await server.add_characteristic(
                uuid=CONTROL_POINT_CHAR_UUID,
                service_uuid=FTMS_SERVICE_UUID,
                properties=BleakGATTCharacteristic.WRITE | BleakGATTCharacteristic.INDICATE,
                write_callback=handle_control_point_write,
            )
            logger.info(f"Added Control Point characteristic: {CONTROL_POINT_CHAR_UUID}")

            logger.info("Services and characteristics added. Waiting for connections...")
            
            # Start periodic bike data updates
            while True:
                await update_bike_data(indoor_bike_data_char)
                await asyncio.sleep(1.0)  # Update every second

    except BleakError as e:
        logger.error(f"Bleak error: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        logger.info("Server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
        sys.exit(0)
