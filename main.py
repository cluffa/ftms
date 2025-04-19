import asyncio
import logging
import sys
import struct
import time
import os

from bumble.device import Device, Peer
from bumble.host import Host
from bumble.gatt import Service, Characteristic, CharacteristicValue
from bumble.core import AdvertisingData
from bumble.transport import open_transport_or_link

# --- Configuration --- (Keep existing UUIDs, DEVICE_NAME, constants, FTMS_FEATURES, ranges, etc.)
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
    0x54, 0x08, 0x00, 0x00,  # Fitness Machine Features
    0xA2, 0x00, 0x00, 0x00,  # Target Setting Features
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00
])

# Supported ranges
RESISTANCE_RANGE = bytearray(struct.pack("<hhh", 0, 3000, 10))
POWER_RANGE = bytearray(struct.pack("<hhh", 0, 2000, 1))

# Training status values
STATUS_IDLE = 0x01
STATUS_ACTIVE = 0x02
STATUS_PAUSED = 0x03
STATUS_COMPLETED = 0x04

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State & Connection Management ---
# Store connection-specific state here
# Key: connection handle or object, Value: dict containing state like 'has_control'
connection_states = {}

# References to characteristics for updates/indications
ftms_service_object = None
indoor_bike_data_char = None
training_status_char = None
control_point_char = None

# Global simulation state (as before)
current_resistance = 0
current_power = 100
current_speed = 1000
current_cadence = 140
current_hr = 0
training_status = STATUS_IDLE

# --- Bumble Specific Functions ---

async def send_control_point_response(connection, request_op_code, response_code, values=None):
    """Sends a response to a control point request via indication (Bumble)."""
    global control_point_char
    if control_point_char is None or connection is None:
        logger.error("Control point characteristic or connection not available")
        return

    # Format is: Response Code (0x80) + Request Op Code + Result Code + [Values]
    response = bytearray([0x80, request_op_code, response_code])
    if values:
        response.extend(values)

    logger.info(f"Sending control point response to {connection.peer_address}: {response.hex()}")
    try:
        # Use the connection object to indicate
        await connection.indicate_characteristic(control_point_char, response)
    except Exception as e:
        logger.error(f"Failed to send indication: {e}")

def generate_indoor_bike_data():
    """Generates a simulated indoor bike data packet according to FTMS spec."""
    # Flags: Speed present, Cadence present, Power present, HR present
    # Adjust flags based on actual available data if needed
    flags = 0b00000000
    data_list = []

    # Speed
    flags |= (1 << 1) # Instantaneous Speed present
    data_list.append(struct.pack("<H", current_speed))

    # Cadence
    flags |= (1 << 2) # Average Cadence present (or Instantaneous if preferred)
    data_list.append(struct.pack("<H", current_cadence))

    # Power
    flags |= (1 << 5) # Instantaneous Power present
    data_list.append(struct.pack("<h", current_power)) # Power is signed

    # HR (Optional)
    if current_hr > 0:
        flags |= (1 << 4) # Heart Rate present
        data_list.append(struct.pack("<B", current_hr))

    # Combine flags and data
    packet = struct.pack("<H", flags) + b''.join(data_list)
    return packet


async def update_bike_data(device):
    """Updates the indoor bike data characteristic for subscribed clients."""
    global indoor_bike_data_char
    if not indoor_bike_data_char or not device:
        return

    try:
        # Simulate changes (as before, or use real sensor data)
        global current_power, current_speed
        # Simplified update logic
        # current_speed = max(500, min(4000, int(1000 + current_resistance * 0.5)))
        # current_power = max(50, min(2000, int(100 + (current_resistance / 10))))

        data = generate_indoor_bike_data()

        # Notify all subscribed connections
        for connection in device.connections.values():
             if connection.is_subscribed(indoor_bike_data_char):
                logger.debug(f"Notifying bike data to {connection.peer_address}")
                await connection.notify_characteristic(indoor_bike_data_char, data)

        logger.debug(f"Updated bike data: speed={current_speed/100}km/h, power={current_power}W, cadence={current_cadence/2}RPM")

    except Exception as e:
        logger.error(f"Error updating bike data: {e}")

# --- Control Point Handler (Bumble) ---
async def on_control_point_write(connection, value):
    """Handles writes to the control point characteristic (Bumble)."""
    global current_resistance, current_power, training_status

    logger.info(f"Received control point command from {connection.peer_address}: {value.hex()}")

    if not value:
        logger.warning("Received empty data on control point.")
        return

    op_code = value[0]
    logger.info(f"  Op Code: {op_code}")

    # Get or initialize state for this connection
    if connection not in connection_states:
        connection_states[connection] = {'has_control': False}
    conn_state = connection_states[connection]

    try:
        if op_code == CP_REQUEST_CONTROL:
            conn_state['has_control'] = True
            logger.info(f"Client {connection.peer_address} requested control - granted")
            await send_control_point_response(connection, CP_REQUEST_CONTROL, RESPONSE_SUCCESS)

        elif op_code == CP_RESET:
            # Reset global state or connection-specific state as appropriate
            current_resistance = 0
            current_power = 100
            training_status = STATUS_IDLE # Reset global training status
            # Reset control for this connection? FTMS spec might clarify
            # conn_state['has_control'] = False
            logger.info("Received reset command")
            await send_control_point_response(connection, CP_RESET, RESPONSE_SUCCESS)
            # Potentially update Training Status char for all subscribed clients

        elif op_code == CP_SET_TARGET_RESISTANCE:
            if not conn_state.get('has_control', False):
                logger.warning(f"Client {connection.peer_address} attempted to set resistance without control")
                await send_control_point_response(connection, CP_SET_TARGET_RESISTANCE, RESPONSE_CONTROL_NOT_PERMITTED)
                return

            if len(value) < 3: # Opcode + 2 bytes for sint16
                logger.warning("Invalid resistance data format")
                await send_control_point_response(connection, CP_SET_TARGET_RESISTANCE, RESPONSE_INVALID_PARAMETER)
                return

            resistance = struct.unpack("<h", value[1:3])[0] # Resistance is sint16 in 0.1 units
            logger.info(f"Setting resistance level to {resistance / 10.0}") # Spec says unitless, often interpreted as %

            min_res, max_res, _ = struct.unpack("<hhh", RESISTANCE_RANGE)
            if resistance < min_res or resistance > max_res:
                logger.warning(f"Resistance value {resistance} out of range ({min_res}-{max_res})")
                await send_control_point_response(connection, CP_SET_TARGET_RESISTANCE, RESPONSE_INVALID_PARAMETER)
                return

            current_resistance = resistance
            # Add code here to physically change resistance if applicable
            # Update power/speed based on new resistance (simulation)
            current_power = max(50, min(2000, int(100 + (resistance / 10)))) # Example update

            await send_control_point_response(connection, CP_SET_TARGET_RESISTANCE, RESPONSE_SUCCESS)

        elif op_code == CP_SET_TARGET_POWER:
            if not conn_state.get('has_control', False):
                logger.warning(f"Client {connection.peer_address} attempted to set power without control")
                await send_control_point_response(connection, CP_SET_TARGET_POWER, RESPONSE_CONTROL_NOT_PERMITTED)
                return

            if len(value) < 3: # Opcode + 2 bytes for sint16
                logger.warning("Invalid power data format")
                await send_control_point_response(connection, CP_SET_TARGET_POWER, RESPONSE_INVALID_PARAMETER)
                return

            power = struct.unpack("<h", value[1:3])[0] # Power is sint16 in Watts
            logger.info(f"Setting target power to {power}W")

            min_pwr, max_pwr, _ = struct.unpack("<hhh", POWER_RANGE)
            if power < min_pwr or power > max_pwr:
                logger.warning(f"Power value {power} out of range ({min_pwr}-{max_pwr})")
                await send_control_point_response(connection, CP_SET_TARGET_POWER, RESPONSE_INVALID_PARAMETER)
                return

            current_power = power
            # Add code here to adjust resistance to meet target power if applicable

            await send_control_point_response(connection, CP_SET_TARGET_POWER, RESPONSE_SUCCESS)

        elif op_code == CP_START_OR_RESUME:
            logger.info("Starting/Resuming training session")
            training_status = STATUS_ACTIVE
            await send_control_point_response(connection, CP_START_OR_RESUME, RESPONSE_SUCCESS)
            # Update Training Status char for all subscribed clients
            # await update_training_status_char(device, training_status) # Need device ref

        elif op_code == CP_STOP_OR_PAUSE:
            if len(value) < 2:
                logger.warning("Invalid stop/pause data format")
                await send_control_point_response(connection, CP_STOP_OR_PAUSE, RESPONSE_INVALID_PARAMETER)
                return

            stop_or_pause = value[1]
            new_status = training_status
            if stop_or_pause == 0x01:  # Stop
                logger.info("Stopping training session")
                new_status = STATUS_IDLE
                # Optionally revoke control on stop
                # conn_state['has_control'] = False
            elif stop_or_pause == 0x02:  # Pause
                logger.info("Pausing training session")
                new_status = STATUS_PAUSED
            else:
                logger.warning(f"Invalid stop/pause parameter: {stop_or_pause}")
                await send_control_point_response(connection, CP_STOP_OR_PAUSE, RESPONSE_INVALID_PARAMETER)
                return

            training_status = new_status
            await send_control_point_response(connection, CP_STOP_OR_PAUSE, RESPONSE_SUCCESS)
            # Update Training Status char for all subscribed clients
            # await update_training_status_char(device, training_status) # Need device ref

        else:
            logger.warning(f"Unsupported op code: {op_code}")
            await send_control_point_response(connection, op_code, RESPONSE_NOT_SUPPORTED)

    except Exception as e:
        logger.error(f"Error handling control point command: {e}", exc_info=True)
        try:
            # Attempt to send a generic failure response
            await send_control_point_response(connection, op_code, RESPONSE_OPERATION_FAILED)
        except Exception as inner_e:
            logger.error(f"Failed to send error response: {inner_e}")


# --- Main Server Logic (Bumble) ---
async def run_server():
    global control_point_char

    logger.info(f"Starting GATT server as '{DEVICE_NAME}'")
    logger.info(f"Service UUID: {FTMS_SERVICE_UUID}")

    # Define transport to use 'hci' for direct hardware access
    transport_name = "hci"
    logger.info(f"Using transport: {transport_name}")

    async with await open_transport_or_link(transport_name) as (hci_source, hci_sink):
        # Use a valid static random address format instead of 'random'
        device = Device(name=DEVICE_NAME, address='F0:F1:F2:F3:F4:F5', host=Host(hci_source, hci_sink))

        # --- Define FTMS Service and Characteristics ---
        feature_char = Characteristic(
            FEATURE_CHAR_UUID,
            Characteristic.Properties.READ,
            Characteristic.Permissions.READ_REQUIRES_AUTHENTICATION, # Or READABLE if no auth needed
            FTMS_FEATURES # Initial value
        )

        resistance_range_char = Characteristic(
            SUPPORTED_RESISTANCE_LEVEL_RANGE_CHAR_UUID,
            Characteristic.Properties.READ,
            Characteristic.Permissions.READABLE,
            RESISTANCE_RANGE
        )

        power_range_char = Characteristic(
            SUPPORTED_POWER_RANGE_CHAR_UUID,
            Characteristic.Properties.READ,
            Characteristic.Permissions.READABLE,
            POWER_RANGE
        )

        # Store refs to chars needed for updates/indications
        indoor_bike_data_char = Characteristic(
            INDOOR_BIKE_DATA_CHAR_UUID,
            Characteristic.Properties.NOTIFY,
            Characteristic.Permissions.READABLE # Notify doesn't have separate permission
        )

        training_status_char = Characteristic(
            TRAINING_STATUS_CHAR_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Characteristic.Permissions.READABLE,
            bytes([training_status]) # Initial value
        )

        control_point_char = Characteristic(
            CONTROL_POINT_CHAR_UUID,
            Characteristic.Properties.WRITE | Characteristic.Properties.INDICATE,
            Characteristic.Permissions.WRITEABLE | Characteristic.Permissions.READABLE # Indicate needs readable? Check spec/Bumble docs
        )
        # Assign the write handler after initialization
        control_point_char.write_value = on_control_point_write

        # Create the FTMS Service
        ftms_service_object = Service(
            FTMS_SERVICE_UUID,
            [
                feature_char,
                resistance_range_char,
                power_range_char,
                indoor_bike_data_char,
                training_status_char,
                control_point_char
            ],
            primary=True
        )

        # Add the service to the device's GATT server
        device.add_service(ftms_service_object)

        # Set advertising data
        advertisement = bytes(AdvertisingData([
            (AdvertisingData.COMPLETE_LOCAL_NAME, bytes(DEVICE_NAME, 'utf-8')),
            (AdvertisingData.INCOMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS, bytes.fromhex(FTMS_SERVICE_UUID.replace('-', ''))),
            (AdvertisingData.FLAGS, bytes([0x06])) # LE General Discoverable Mode, BR/EDR Not Supported
        ]))
        # Assign directly to the attribute instead of calling a method
        device.advertising_data = advertisement

        # Debug: Print services and characteristics
        for service in device.gatt_server.services:
            logger.info(f"Service {service.uuid}:")
            for char in service.characteristics:
                logger.info(f"  Characteristic {char.uuid} Properties: {char.properties} Permissions: {char.permissions}")

        # Start advertising
        await device.start_advertising(auto_restart=True)
        logger.info(f"Advertising as '{DEVICE_NAME}'...")

        # Start periodic updates
        async def periodic_update_task():
            while True:
                await update_bike_data(device)
                # Update training status char if needed
                # await update_training_status_char(device, training_status)
                await asyncio.sleep(1.0)

        update_task = asyncio.create_task(periodic_update_task())

        # Keep the server running until interrupted
        await asyncio.get_running_loop().create_future()

        # Cleanup (though create_future() runs forever unless cancelled)
        update_task.cancel()
        await device.stop_advertising()


if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        sys.exit(0)
