import asyncio
import logging
import sys

from bleak import BleakServer, BleakGATTCharacteristic, BleakError

# --- Configuration ---
# Use a custom UUID for the service and characteristic for now.
# Replace these with the official FTMS UUIDs later.
# FTMS Service UUID: 00001826-0000-1000-8000-00805f9b34fb
# Fitness Machine Control Point UUID: 00002AD9-0000-1000-8000-00805f9b34fb
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CONTROL_POINT_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
DEVICE_NAME = "DIY FTMS Bike"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Control Point Handler ---
# This function will be called when a client writes to the control point characteristic
def handle_control_point_write(sender: int, data: bytearray):
    """Handles writes to the control point characteristic."""
    logger.info(f"Received control point command: {data.hex()}")
    # TODO: Parse the FTMS command according to the specification
    # Example: Check Op Code (first byte)
    if not data:
        logger.warning("Received empty data on control point.")
        return

    op_code = data[0]
    logger.info(f"  Op Code: {op_code}")

    # --- Add logic here to handle specific op codes ---
    # e.g., Request Control, Reset, Set Target Resistance, Set Target Power etc.
    # For now, we just log the raw command.
    # You would parse data[1:] based on the op_code.

    # Example: Placeholder for Set Target Resistance Level (Op Code 0x04)
    if op_code == 0x04 and len(data) >= 2:
        # Resistance level is a sint16, resolution 0.1 (signed 16-bit integer)
        # Note: FTMS uses little-endian format.
        # This simple example assumes unsigned byte for simplicity.
        resistance_level = data[1] # Simplified - needs proper sint16 parsing
        logger.info(f"  Attempting to set resistance level: {resistance_level}")
        # --- Add code here to control your bike's resistance mechanism ---

    # TODO: Send back a response via indication if required by the FTMS spec
    # (Requires setting up indications on the characteristic)


# --- Main Server Logic ---
async def run_server():
    """Runs the BLE GATT server."""
    logger.info(f"Starting GATT server as '{DEVICE_NAME}'")
    logger.info(f"Service UUID: {SERVICE_UUID}")
    logger.info(f"Control Point Characteristic UUID: {CONTROL_POINT_CHAR_UUID}")

    try:
        async with BleakServer(DEVICE_NAME) as server:
            logger.info("Server started. Advertising...")

            # Add the service and characteristic
            await server.add_service(uuid=SERVICE_UUID, is_primary=True)
            await server.add_characteristic(
                uuid=CONTROL_POINT_CHAR_UUID,
                service_uuid=SERVICE_UUID,
                properties=BleakGATTCharacteristic.WRITE_WITHOUT_RESPONSE | BleakGATTCharacteristic.WRITE | BleakGATTCharacteristic.INDICATE, # Added Indicate for responses
                write_callback=handle_control_point_write,
                # read_callback=None, # Add if needed
                # notify_callback=None # Add if needed for notifications
            )

            logger.info("Service and characteristic added. Waiting for connections...")
            # Keep the server running indefinitely
            await asyncio.Event().wait()

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
