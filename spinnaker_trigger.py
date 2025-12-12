"""
Minimal Spinnaker capture helper with optional hardware trigger.
Saves a video via SpinVideo when available; otherwise dumps PNG frames.
Every line is commented for clarity.
"""

from __future__ import annotations  # allow forward type hints

import pathlib  # for filesystem paths

import PySpin  # Teledyne Spinnaker Python SDK
try:
    import cv2  # OpenCV for AVI writing fallback
except ImportError:
    cv2 = None


# --------------------------- user parameters ---------------------------
NUM_FRAMES = 300  # total frames to capture
EXPOSURE_TIME_US = 10000.0  # manual exposure in microseconds
VIDEO_PATH = pathlib.Path("camera_capture.avi")  # where to save the video/frames
TIMEOUT_MS = 20000  # GetNextImage timeout in milliseconds
PIXEL_FORMAT = PySpin.PixelFormat_Mono8  # requested pixel format
VIDEO_FRAME_RATE = 30.0  # frame rate metadata for SpinVideo
VIDEO_QUALITY = 100  # SpinVideo quality (1-100)
BINNING_FACTOR = 1  # 1 = full resolution, 2 = 2x2 binning, etc.


def configure_hardware_trigger(cam: PySpin.CameraPtr) -> None:
    cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)  # disable trigger while configuring
    cam.LineSelector.SetValue(PySpin.LineSelector_Line0)  # select opto-isolated input line
    cam.LineMode.SetValue(PySpin.LineMode_Input)  # ensure Line3 is configured as input
    cam.TriggerSelector.SetValue(PySpin.TriggerSelector_FrameStart)  # fire on frame start
    cam.TriggerSource.SetValue(PySpin.TriggerSource_Line0)  # external opto-in triggers frames
    cam.TriggerActivation.SetValue(PySpin.TriggerActivation_RisingEdge)  # rising edge trigger
    if hasattr(cam, "TriggerOverlap"):  # set overlap if supported
        cam.TriggerOverlap.SetValue(PySpin.TriggerOverlap_ReadOut)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_On)  # re-enable trigger


def configure_freerun(cam: PySpin.CameraPtr) -> None:
    cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)  # disable triggering for free-run
    cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)  # continuous capture


def configure_exposure(cam: PySpin.CameraPtr, exposure_us: float, max_exposure_us: float | None = None) -> None:
    """Set manual exposure, optionally capped by a max value (e.g., frame period)."""
    cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)  # turn off auto exposure
    cam.ExposureMode.SetValue(PySpin.ExposureMode_Timed)  # use timed exposure
    node = cam.ExposureTime  # exposure node
    target = exposure_us
    if max_exposure_us is not None:  # cap by requested max
        target = min(target, max_exposure_us)
    clamped = min(max(node.GetMin(), target), node.GetMax())  # clamp to device limits
    node.SetValue(clamped)  # apply exposure


def configure_binning(cam: PySpin.CameraPtr, binning_factor: int) -> None:
    """
    Set horizontal and vertical binning to the specified factor.
    Note: Some cameras require BinningSelector to be set (e.g. 'All' or 'Sensor') first.
    """
    if binning_factor <= 1:
        return

    # Try setting BinningSelector to 'All' or 'Sensor' if available
    # This ensures we are binning the sensor data
    if hasattr(cam, "BinningSelector") and cam.BinningSelector.GetAccessMode() == PySpin.RW:
        # Try 'All', 'Sensor'
        # Note: These enums might vary by camera model. Using string or skipping if specific ones not found.
        # Often default is fine, but being explicit is better if possible.
        pass

    # Set Horizontal Binning
    if hasattr(cam, "BinningHorizontal") and cam.BinningHorizontal.GetAccessMode() == PySpin.RW:
        node = cam.BinningHorizontal
        target = binning_factor
        clamped = min(max(node.GetMin(), target), node.GetMax())
        node.SetValue(clamped)
    else:
        print(f"Warning: BinningHorizontal not writable or unavailable.")

    # Set Vertical Binning
    if hasattr(cam, "BinningVertical") and cam.BinningVertical.GetAccessMode() == PySpin.RW:
        node = cam.BinningVertical
        target = binning_factor
        clamped = min(max(node.GetMin(), target), node.GetMax())
        node.SetValue(clamped)
    else:
        print(f"Warning: BinningVertical not writable or unavailable.")


def acquire_triggered_frames(
    num_frames: int = NUM_FRAMES,
    video_path: pathlib.Path = VIDEO_PATH,
    timeout_ms: int = TIMEOUT_MS,
    pixel_format: PySpin.PixelFormatEnums = PIXEL_FORMAT,
    exposure_us: float = EXPOSURE_TIME_US,
    video_frame_rate: float = VIDEO_FRAME_RATE,
    video_quality: int = VIDEO_QUALITY,
    binning_factor: int = BINNING_FACTOR,
    use_trigger: bool = True,
):
    print(f"Spinnaker capturing {num_frames} frames")
    video_path = pathlib.Path(video_path)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    tiff_dir = video_path.parent / (video_path.stem + "_frames")

    system = None
    cam_list = None
    cam = None
    try:
        system = PySpin.System.GetInstance()
        cam_list = system.GetCameras()
        if cam_list.GetSize() == 0:
            raise RuntimeError("No Spinnaker cameras detected.")

        cam = cam_list[0]
        cam.Init()

        if use_trigger:
            configure_hardware_trigger(cam)
        else:
            configure_freerun(cam)

        configure_binning(cam, binning_factor)

        frame_period_us = 1e6 / video_frame_rate if video_frame_rate > 0 else None
        max_exp = frame_period_us if frame_period_us is not None else None
        configure_exposure(cam, exposure_us, max_exp)
        cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)

        use_video_writer = hasattr(PySpin, "SpinVideo") and hasattr(PySpin, "SpinVideoOptions")
        video_writer = None
        cv_writer = None
        if use_video_writer:
            video_writer = PySpin.SpinVideo()
            opts = PySpin.SpinVideoOptions()
            opts.frameRate = video_frame_rate
            opts.quality = max(1, min(100, video_quality))
            video_writer.Open(str(video_path), opts)
        else:
            tiff_dir.mkdir(parents=True, exist_ok=True)

        cam.BeginAcquisition()
        frames_saved = 0
        try:
            while frames_saved < num_frames:
                image = cam.GetNextImage(timeout_ms)
                try:
                    if image.IsIncomplete():
                        print(f"Frame {frames_saved} incomplete: {image.GetImageStatus()}")
                        continue
                    if hasattr(image, "Convert"):
                        image = image.Convert(pixel_format)
                    if use_video_writer and video_writer:
                        video_writer.Append(image)
                    elif cv2 is not None:
                        nd = image.GetNDArray()
                        if cv_writer is None:
                            h, w = nd.shape[:2]
                            is_color = nd.ndim == 3 and nd.shape[2] == 3
                            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
                            cv_writer = cv2.VideoWriter(str(video_path), fourcc, video_frame_rate, (w, h), is_color)
                        cv_writer.write(nd)
                    else:
                        tiff_path = tiff_dir / f"frame_{frames_saved:04d}.tiff"
                        image.Save(str(tiff_path))
                    frames_saved += 1
                    print(f"Captured frame {frames_saved}/{num_frames}")
                finally:
                    image.Release()
        finally:
            try:
                cam.EndAcquisition()
            except PySpin.SpinnakerException as exc:
                print(f"Warning: EndAcquisition failed: {exc}")
            cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)
            if use_video_writer and video_writer:
                video_writer.Close()
            if cv_writer is not None:
                cv_writer.release()
            video_writer = None
            cv_writer = None
    except PySpin.SpinnakerException as exc:
        print(f"Spinnaker error: {exc}")
    finally:
        if cam is not None:
            try:
                cam.DeInit()
            except PySpin.SpinnakerException as exc:
                print(f"Warning: DeInit failed: {exc}")
        if cam_list is not None:
            try:
                cam_list.Clear()
            except PySpin.SpinnakerException as exc:
                print(f"Warning: cam_list.Clear failed: {exc}")
        cam = None
        cam_list = None
        system = None


if __name__ == "__main__":  # script entrypoint
    acquire_triggered_frames(use_trigger=True)  # free-run demo for bench testing
