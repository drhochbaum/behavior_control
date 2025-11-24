## Virtual LabJack + Camera System

This repository hosts a lightweight Python simulation of an experimental setup that:

- Fires a digital start pulse (LabJack → camera trigger)
- Drives an LED driver with a configurable analog sine wave
- Captures a photodetector analog signal and a camera frame TTL pulse
- Keeps every signal synchronized to a shared simulation clock
- Provides an interactive GUI so you can tweak parameters and observe the resulting waveforms

### Requirements

- Python 3.9+ with the standard library `tkinter` module available (most macOS and Linux distributions include it by default).

### Running the simulator

```bash
python3 simulator.py
```

This launches the GUI. Use the sliders/spinbox on the left to set the LED sine-wave frequency, amplitude, and camera frame rate. Press **Send Start Pulse** to simulate a LabJack digital edge that triggers the camera recording session. The right-hand side plots show three synchronized traces: LED analog voltage, photodetector response, and camera TTL pulses (all sharing the same simulated clock).

### Programmatic launch

If you prefer to embed the GUI in another script, import `launch_gui` from `simulator.py`:

```python
from simulator import launch_gui

if __name__ == "__main__":
    launch_gui()
```

### How it works

`SimulationCore` keeps a deterministic timebase (default 10 ms steps) and updates the digital start pulse, LED waveform, photodetector response, and camera TTL pulses on every tick. The GUI pulls the latest samples via `after()` callbacks, updates status indicators, and redraws the rolling history plot so you can observe the entire virtual experiment in real time.

## Controlling a real LabJack via LJM

The `labjack_control.py` module uses the LabJack [LJM Python library](https://labjack.com/pages/support?doc=/software-driver/ljm-users-guide/ljm-users-guide/) to talk to physical hardware. It mirrors the simulator’s functionality:

- Drive the LED driver with a sine wave on `DAC0`
- Send a digital start pulse on `FIO0`
- Poll the photodetector (`AIN0`), camera TTL input (`AIN1`), and the LED monitor tap (`AIN2`) on a shared clock

### Requirements

1. Install the native LabJack LJM driver for your OS (from LabJack’s website).
2. Install the Python bindings:

   ```bash
   python3 -m pip install labjack-ljm
   ```

### Quick demo

```bash
python3 labjack_control.py
```

This runs `demo_run()`, which:

1. Opens the first available LJM-compatible device.
2. Starts a 20 Hz sine wave on `DAC0`.
3. Begins polling the photodetector and camera TTL inputs at 200 Hz.
4. Fires a single start pulse on `FIO0`.
5. Logs a few recent samples to the console.

### Embedding in your own scripts

```python
from labjack_control import LabJackController

with LabJackController() as controller:
    controller.start_led_sine(frequency_hz=50, amplitude_v=2.0, offset_v=2.5)
    controller.start_capture(sample_rate_hz=1000.0)
    controller.send_start_pulse(pulse_width_s=0.01)
    time.sleep(2.0)
    samples = controller.latest_samples(200)
```

Adjust channel names when instantiating `LabJackController` if your wiring uses different DAC/AIN/FIO lines.

### Hardware-clocked streaming

If you need every sample to be paced by the LabJack’s internal clock (instead of the PC’s scheduler), look at `labjack_stream_control.py`. That file sketches how to:

- Configure `STREAM_OUT0` to replay a sine waveform on `DAC0`
- Add the photodetector (`AIN0`), LED monitor (`AIN2`), and camera TTL (`AIN1`) to the `STREAM_SCAN_LIST`
- Start the device’s stream engine via `ljm.eStreamStart`, fire a synchronized camera-start TTL pulse on `FIO0`, and pull evenly spaced data blocks with `ljm.eStreamRead`

Because the stream engine handles timing, your LED waveform, TTL captures, and photodetector samples stay phase-locked to the same hardware timebase. Use this skeleton as a starting point if you need deterministic sampling at (for example) 4 kHz with a 250 Hz LED drive. Each run of `run_pulsed_stream()` also logs all scanned channels (AIN0/1/2 plus the stream-out registers) to `labjack_stream.csv` by default; edit `DATA_LOG_PATH` in `labjack_stream_control.py` or pass a different `log_path` when invoking the helper if you want another destination.

## Configuring the FLIR/Teledyne camera (Spinnaker)

The `spinnaker_trigger.py` script wraps the official [Spinnaker-Examples](https://github.com/Teledyne-MV/Spinnaker-Examples) best practices for configuring a Flea/BFS camera:

- Sets `TriggerSource = Line0`, `TriggerSelector = FrameStart`, `TriggerActivation = RisingEdge`, `TriggerOverlap = ReadOut` to ensure each LabJack pulse starts one frame.
- Disables auto-exposure and programs a manual `ExposureTime` (µs) so the sensor integrates no longer than the trigger period.
- Starts acquisition, waits for each hardware trigger via `GetNextImage()`, converts to `Mono8`, and appends each frame to a single `.avi` file via `PySpin.SpinVideo`.

Run it with:

```bash
python3 spinnaker_trigger.py
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
   pip install /path/to/spinnaker_python-<...>-cp3xx-<...>-arm64.whl  # from the SDK
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

## Auxiliary

- The old simulator and software-timed `labjack_control.py` have been moved into the `Old/` folder and are no longer used in this pipeline.

## Full experiment wrapper

`run_experiment.py` coordinates both the camera and hardware stack:

1. Starts `spinnaker_trigger.acquire_triggered_frames()` in a background thread so the camera enters trigger mode.
2. Waits briefly, then calls `labjack_stream_control.run_pulsed_stream()` so the LabJack generates the LED sine wave, captures photodetector data, and emits a hardware-streamed TTL pulse train (STREAM_OUT1 driving `FIO0`) with exactly the same number of pulses as `NUM_FRAMES`.

Edit the parameter block at the top of `run_experiment.py` (frame count, trigger rate, exposure time, LabJack log path) and run:

```bash
python3 run_experiment.py
```

This ensures each LabJack pulse captures one hardware-triggered image, and both filesystems (camera images + analog traces) remain synchronized.
