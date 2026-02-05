# Dependencies: matplotlib, numpy, horiba-sdk

"""
Takes a single image acquisition and displays as an image of a 2D array.
"""

import asyncio
import sys
import traceback

try:
    import matplotlib.pyplot as plt
    import numpy as np
    from loguru import logger
    
    from horiba_sdk.core.acquisition_format import AcquisitionFormat
    from horiba_sdk.core.timer_resolution import TimerResolution
    from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType
    from horiba_sdk.devices.device_manager import DeviceManager
except ImportError as e:
    print(f"missing dependency. {e}")
    sys.exit(1)


async def main():
    logger.info("starting Device Manager...")
    device_manager = DeviceManager(start_icl=True)
    await device_manager.start()

    if not device_manager.charge_coupled_devices:
        logger.error("no CCD detected via DeviceManager")
        await device_manager.stop()
        return

    ccd = device_manager.charge_coupled_devices[0]
    
    try:
        await ccd.open()
        logger.info(f"CCD opened successfully: {ccd}")

        ccd_config = await ccd.get_configuration()
        chip_x = int(ccd_config["chipWidth"])
        chip_y = int(ccd_config["chipHeight"])
        logger.info(f"Chip dimensions: {chip_x} x {chip_y}")

        try:
            acquisition_format = AcquisitionFormat.IMAGE
        except AttributeError:
            formats = [attr for attr in dir(AcquisitionFormat) if not attr.startswith('_')]
            if not formats:
                raise RuntimeError("Could not determine valid AcquisitionFormat")
            acquisition_format = getattr(AcquisitionFormat, formats[0])
            logger.info(f"AcquisitionFormat.IMAGE not found, using: {formats[0]}")

        await ccd.set_acquisition_format(1, acquisition_format)
        
        await ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, 1)
        await ccd.set_x_axis_conversion_type(XAxisConversionType.NONE)
        await ccd.set_acquisition_count(1)

        exposure_time = 1000  # ms
        await ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
        await ccd.set_exposure_time(exposure_time)

        logger.info(f"Starting acquisition with {exposure_time}ms exposure")
        await ccd.acquisition_start(open_shutter=True)

        total_wait = (exposure_time / 1000) + 2.0
        logger.info(f"Waiting {total_wait:.1f}s for acquisition...")
        await asyncio.sleep(total_wait)

        raw_data = await ccd.get_acquisition_data()
        
        if not raw_data.get("acquisition"):
            raise ValueError("Acquisition completed but returned no data.")

        acquisition = raw_data["acquisition"][0]
        region = acquisition.get("regions", acquisition.get("roi"))[0]
        image_data = np.array(region["yData"])
        
        logger.success("Image acquired successfully")
        
    except Exception as e:
        logger.error(f"Acquisition failed: {e}")
        traceback.print_exc()
        image_data = None
    finally:
        if 'ccd' in locals() and ccd:
            await ccd.close()
        await device_manager.stop()
        logger.info("CCD closed and ICL stopped")

    if image_data is not None:
        plot_image(image_data)
    else:
        logger.warning("Skipping plot due to missing data.")


def plot_image(image_array):
    plt.figure(figsize=(12, 6))
    im = plt.imshow(image_array, interpolation="nearest", aspect="auto", 
                    cmap='viridis', origin='lower')
    plt.colorbar(im, label="Intensity (counts)")
    plt.title("CCD Image")
    plt.xlabel("X pixels")
    plt.ylabel("Y pixels")
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    asyncio.run(main())