import asyncio
import numpy as np
import matplotlib.pyplot as plt
from loguru import logger
import functools
from horiba_sdk.core.acquisition_format import AcquisitionFormat
from horiba_sdk.core.timer_resolution import TimerResolution
from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType
from horiba_sdk.devices.device_manager import DeviceManager

import websockets
websockets.connect = functools.partial(websockets.connect, max_size=None, ping_interval=None)

async def main():
    logger.info("Starting Device Manager...")
    device_manager = DeviceManager(start_icl=True)
    await device_manager.start()

    if not device_manager.charge_coupled_devices:
        logger.error("No CCD detected.")
        await device_manager.stop()
        return

    ccd = device_manager.charge_coupled_devices[0]
    await ccd.open()
    logger.info(f"CCD Opened: {ccd}")

    try:
        config = await ccd.get_configuration()
        chip_x = int(config.get("chipWidth", 1024))
        chip_y = int(config.get("chipHeight", 256))
        logger.info(f"Chip Dimensions: {chip_x} x {chip_y}")

        await ccd.set_acquisition_format(1, AcquisitionFormat.IMAGE)
        await ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, 1)
        await ccd.set_x_axis_conversion_type(XAxisConversionType.NONE)
        await ccd.set_acquisition_count(1)
        
        exposure_time = 1000 
        await ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
        await ccd.set_exposure_time(exposure_time)

        logger.info(f"Starting acquisition ({exposure_time}ms)...")
        await ccd.acquisition_start(open_shutter=True)

        wait_time = (exposure_time / 1000) + 7.0
        logger.info(f"Waiting {wait_time:.1f}s...")
        await asyncio.sleep(wait_time)

        raw_data = await ccd.get_acquisition_data()
        
        if isinstance(raw_data, list):
            raw_data = raw_data[0]
        
        if 'roi' in raw_data:
            roi_data = raw_data['roi'][0]
        elif 'acquisition' in raw_data:
            roi_data = raw_data['acquisition'][0]['roi'][0]
        else:
            raise ValueError(f"Unknown data keys: {raw_data.keys()}")

        image_data = np.array(roi_data['yData'])
        if image_data.ndim == 1:
            image_data = image_data.reshape(chip_y, chip_x)
            
        logger.success("image acquired")
        plot_image(image_data)

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await ccd.close()
        await device_manager.stop()
        logger.info("Closed.")

def plot_image(image_array):
    plt.figure(figsize=(12, 6))
    im = plt.imshow(image_array, interpolation="nearest", aspect="auto", 
                    cmap='viridis', origin='lower')
    plt.colorbar(im, label="Intensity (counts)")
    plt.title("CCD Image")
    plt.xlabel("X pixels")
    plt.ylabel("Y pixels")
    
    plt.show()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass