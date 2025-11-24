## Controlling a LabJack via LJM

The `labjack_stream_control.py` module uses the LabJack [LJM Python library](https://labjack.com/pages/support?doc=/software-driver/ljm-users-guide/ljm-users-guide/) to talk to physical hardware. It’s functionality:

- Drive the LED driver with a sine wave on `DAC0`
- Send a digital start pulse on `FIO0`
- Poll the photodetector (`AIN0`), camera TTL input (`AIN1`), and the LED driver (`AIN2`) on a shared clock

### Requirements

1. Install the native LabJack LJM driver for your OS (from LabJack’s website).
2. Install the Python bindings:

   ```bash
   pip install labjack-ljm
   ```

### Hardware-clocked streaming details

`labjack_stream_control.py`:

- Configure `STREAM_OUT0` to replay a sine waveform on `DAC0`
- Add the photodetector (`AIN0`), camera TTL (`AIN1`), and LED driver monitor (`AIN2`)  to the `STREAM_SCAN_LIST`
- Start the device’s stream engine via `ljm.eStreamStart`, fire a synchronized camera-start TTL pulse on `FIO0`, and pull evenly spaced data blocks with `ljm.eStreamRead`

Because the stream engine handles timing, your LED waveform, TTL captures, and photodetector samples stay phase-locked to the same hardware timebase. Each run of `run_pulsed_stream()` also logs all scanned channels (AIN0/1/2 plus the stream-out registers) to `labjack_stream.csv` by default; edit `DATA_LOG_PATH` in `labjack_stream_control.py` or pass a different `log_path` when invoking the helper if you want another destination.

## Configuring the FLIR/Teledyne camera (Spinnaker)

The `spinnaker_trigger.py` script wraps the official [Spinnaker-Examples](https://github.com/Teledyne-MV/Spinnaker-Examples) best practices for configuring a Flea/BFS camera:

- Sets `TriggerSource = Line0`, `TriggerSelector = FrameStart`, `TriggerActivation = RisingEdge`, `TriggerOverlap = ReadOut` to ensure each LabJack pulse starts one frame.
- Disables auto-exposure and programs a manual `ExposureTime` (µs) so the sensor integrates no longer than the trigger period.
- Starts acquisition, waits for each hardware trigger via `GetNextImage()`, converts to `Mono8`, and appends each frame to a single `.avi` file via `PySpin.SpinVideo`or via openCV2.

Run it with:

```bash
python spinnaker_trigger.py
```

Before launching, make sure your LabJack start pulse (`FIO0`) is wired to the Spinnaker camera’s trigger input and that the Spinnaker SDK is installed. Update the parameter block in the script (`NUM_FRAMES`, `EXPOSURE_TIME_US`, `VIDEO_PATH`, `VIDEO_FRAME_RATE`) to match your experiment.

## Environment setup (LabJack + Spinnaker)

Set up a fresh Python env (example uses Conda + Python 3.10) with the versions that work with PySpin and LJM:

1. Install native drivers/SDKs:
   - LabJack LJM driver (from LabJack’s site)
   - Teledyne Spinnaker SDK for your platform (arm64 on Apple Silicon) which includes the matching PySpin wheel

2. Create/activate an env and pin the key packages:

   ```bash
   conda create -n behavior_rig python=3.10
   conda activate behavior_rig
   pip install "numpy<2"
   pip install opencv-python==4.8.1.78    # any <4.9 build is fine
   pip install labjack-ljm
   pip install /path/to/spinnaker_python-<...>-cp3xx-<...>-arm64.whl  # Install correct wheel from the SDK, depends on your computer (arm64 for new Mac)
   ```

3. If PySpin isn’t found, add the SDK’s Python folder to `PYTHONPATH`, e.g.:

   ```bash
   export PYTHONPATH="/Applications/Teledyne DALSA/Spinnaker/lib/python3.10:$PYTHONPATH"
   ```

4. Verify imports:

   ```bash
   python - <<'PY'
   import PySpin, cv2, labjack.ljm, numpy
   print("PySpin SpinVideoOptions:", hasattr(PySpin, "SpinVideoOptions"))
   print("NumPy:", numpy.__version__)
   print("OpenCV:", cv2.__version__)
   PY
   ```

Wiring note: the camera trigger uses the non-isolated input (Red, pin3, Line0 in software). Tie the LabJack trigger line to Line0 and share a ground with the camera.

## Full experiment wrapper

`run_experiment.py` coordinates both the camera and hardware stack:

1. Starts `spinnaker_trigger.acquire_triggered_frames()` in a background thread so the camera enters trigger mode.
2. Waits briefly, then calls `labjack_stream_control.run_pulsed_stream()` so the LabJack generates the LED sine wave, captures photodetector data, and emits a hardware-streamed TTL pulse train (STREAM_OUT1 driving `FIO0`) with exactly the same number of pulses as `NUM_FRAMES`.

Edit the parameter block at the top of `run_experiment.py` (frame count, trigger rate, exposure time, LabJack log path) and run:

```bash
python run_experiment.py
```

This ensures each LabJack pulse captures one hardware-triggered image, and both filesystems (camera images + analog traces) remain synchronized.
