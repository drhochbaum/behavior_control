"""
Sketch of a LabJack controller that uses the hardware stream engine so all timing
is derived from the device's internal clock rather than the host PC. The goal is
to keep the photodetector samples, camera TTL captures, and LED sine-wave output
perfectly synchronized, including a one-shot digital pulse that kicks the camera
when the sine wave starts.

This code is intentionally verbose and highly commented to highlight the moving
pieces you need to configure for a real deployment.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
import time
from typing import List, Sequence, Tuple

from labjack import ljm
from labjack.ljm import errorcodes

# ---------------------------------------------------------------------------
# Device-level configuration (update to taste)
# ---------------------------------------------------------------------------
STREAM_SCAN_RATE_HZ = 4000.0            # Master sample rate (LabJack hardware clock)
SCANS_PER_READ = 400                    # Number of scans pulled per eStreamRead call
LED_FREQUENCY_HZ = 250.0                # Sine wave that drives the LED
LED_AMPLITUDE_V = 2.0
LED_OFFSET_V = 2.5
LED_IDLE_V = 0.0
DATA_LOG_PATH = Path("labjack_stream.csv")  # Default location for streamed data

# Channel mapping assumptions (T7 / T8)
PHOTODETECTOR_AIN = "AIN0"
CAMERA_TTL_AIN = "AIN1"                 # TTL digitized as analog (0 V / 5 V)
LED_MONITOR_AIN = "AIN2"                # Optional feedback of the LED drive waveform
LED_DAC_CHANNEL = "DAC0"
STREAM_OUT_INDEX = 0                    # Use STREAM_OUT0 to drive DAC0
TTL_STREAM_OUT_INDEX = 1                # STREAM_OUT1 drives the TTL trigger
START_PULSE_CHANNEL = "FIO0"            # Digital line that fires the camera
START_PULSE_TARGET = "FIO_STATE"        # Port register for stream-out targeting FIO pins
FIO0_MASK_ALLOW = 0xFE                  # Mask that allows only FIO0 to change
START_PULSE_WIDTH_S = 0.001             # Pulse duration (seconds)
CAMERA_TRIGGER_RATE_HZ = 30.0           # Default pulse train frequency (Hz)
NUM_CAMERA_FRAMES = 300                 # Default number of TTL pulses/frames


@dataclass
class StreamConfig:
    """Bundles the derived parameters for starting an LJM stream."""

    scan_rate_hz: float
    scans_per_read: int
    input_names: Sequence[str]
    stream_out_names: Sequence[str]

    def build_scan_list(self, enabled_stream_outs: Sequence[str]) -> List[str]:
        # enabled_stream_outs is a sequence of stream-out register names (strings).
        # The return type List[str] is the final scan list (input channels + stream outs).
        return list(self.input_names) + list(enabled_stream_outs)


class StreamedLabJackController:
    """
    Coordinates the LabJack stream engine for:
        - hardware-timed sampling of AIN0 (photodetector) and AIN1 (camera TTL)
        - hardware-timed waveform generation on DAC0 via STREAM_OUT0
    """

    def __init__(
        self,
        device_type: str = "ANY",
        connection_type: str = "ANY",
        identifier: str = "ANY",
    ):
        self.handle = ljm.openS(device_type, connection_type, identifier)
        self.device_type = ljm.getHandleInfo(self.handle)[0]
        self.stream_config = StreamConfig(
            scan_rate_hz=STREAM_SCAN_RATE_HZ,
            scans_per_read=SCANS_PER_READ,
            input_names=[PHOTODETECTOR_AIN, CAMERA_TTL_AIN, LED_MONITOR_AIN],
            stream_out_names=[f"STREAM_OUT{STREAM_OUT_INDEX}"],
        )
        self._scan_list_addresses: List[int] = []  # Populated later based on enabled I/O
        self._actual_scan_rate = self.stream_config.scan_rate_hz  # Updated after eStreamStart
        self._ttl_waveform: List[int] | None = None  # Holds the DIO stream-out pattern
        self._ttl_duration_s: float = 0.0  # Duration of the TTL train in seconds
        self._ttl_num_pulses: int = 0  # Number of TTL pulses requested
        self._configure_channels()
        self._num_inputs = len(self.stream_config.input_names)

    # ------------------------------------------------------------------ setup
    def _configure_channels(self):
        """Apply analog range / settling settings once before streaming."""
        if self.device_type == ljm.constants.dtT4:
            # T4: only a few AIN support ranges; keep defaults.
            ljm.eWriteNames(
                self.handle,
                2,
                ["STREAM_SETTLING_US", "STREAM_RESOLUTION_INDEX"],
                [0, 0],
            )
        else:
            # T7/T8: disable trigger, use internal clock, set +/-10 V range, single-ended.
            try:
                ljm.eStreamStop(self.handle)  # ensure no prior stream is active
            except ljm.LJMError:
                pass
            ljm.eWriteName(self.handle, "STREAM_TRIGGER_INDEX", 0)  # ensure stream is free-running
            ljm.eWriteName(self.handle, "STREAM_CLOCK_SOURCE", 0)  # use the internal clock
            self._write_optional_register("DIO_ANALOG_ENABLE", 0)  # force all DIO to digital mode
            a_names = ["STREAM_RESOLUTION_INDEX", "STREAM_SETTLING_US"]  # global stream settings
            a_values = [0, 0]
            for ain in self.stream_config.input_names:
                a_names.extend([f"{ain}_RANGE", f"{ain}_NEGATIVE_CH"])  # per-channel config
                a_values.extend([10.0, 199])  # +/-10 V single-ended for each AIN
            ljm.eWriteNames(self.handle, len(a_names), a_names, a_values)  # batch write the config

        self._configure_fio0_direction()
        # Ensure DAC sits at a safe idle level before the stream starts.
        ljm.eWriteName(self.handle, LED_DAC_CHANNEL, LED_IDLE_V)
        # Default the camera start line low.
        self._set_fio0_state(False)
        self._scan_list_addresses = []

    # ------------------------------------------------------------------ LED waveform
    def configure_led_stream_out(self):
        """
        Load a sine wave into STREAM_OUT0 so the DAC plays back from the hardware
        buffer in sync with the scan clock.
        """
        samples_per_period = int(STREAM_SCAN_RATE_HZ / LED_FREQUENCY_HZ)
        if samples_per_period < 2:
            raise ValueError("Scan rate must be at least 2x the LED frequency.")

        waveform = [
            LED_OFFSET_V + LED_AMPLITUDE_V * math.sin((2 * math.pi * i) / samples_per_period)
            for i in range(samples_per_period)
        ]

        self._configure_stream_out(STREAM_OUT_INDEX, LED_DAC_CHANNEL, waveform, loop=True)
        print(f"STREAM_OUT{STREAM_OUT_INDEX} buffer ready ({len(waveform)} samples).")

    def prepare_ttl_waveform(self, num_pulses: int, rate_hz: float, pulse_width_s: float):
        """Construct a hardware-timed TTL waveform for the stream engine."""
        if num_pulses <= 0:
            self._ttl_waveform = None
            self._ttl_duration_s = 0.0
            self._ttl_num_pulses = 0
            return

        samples_per_period = max(1, int(round(STREAM_SCAN_RATE_HZ / max(rate_hz, 0.1))))
        high_samples = max(1, int(round(pulse_width_s * STREAM_SCAN_RATE_HZ)))
        high_samples = min(high_samples, samples_per_period)

        high_word = (FIO0_MASK_ALLOW << 8) | 0x01
        low_word = (FIO0_MASK_ALLOW << 8)
        waveform: List[int] = []
        waveform.extend([high_word] * high_samples)
        waveform.extend([low_word] * (samples_per_period - high_samples))

        self._ttl_num_pulses = num_pulses
        self._ttl_waveform = waveform
        self._ttl_duration_s = num_pulses / max(rate_hz, 0.1)

    def configure_ttl_stream_out(self):
        """Load the TTL waveform into STREAM_OUT1 targeting the camera trigger line."""
        if not self._ttl_waveform:
            return
        self._configure_stream_out(
            TTL_STREAM_OUT_INDEX,
            START_PULSE_TARGET,
            self._ttl_waveform,
            loop=True,
            value_type="u16",
        )

    @property
    def ttl_duration_seconds(self) -> float:
        return self._ttl_duration_s

    def _configure_stream_out(
        self,
        index: int,
        target_name: str,
        waveform: List[float] | List[int],
        loop: bool,
        value_type: str = "f32",
    ):
        base = f"STREAM_OUT{index}"
        target_addr = ljm.nameToAddress(target_name)[0]
        ljm.eWriteName(self.handle, f"{base}_ENABLE", 0)
        buffer_size = max(512, 2 ** int(math.ceil(math.log2(max(1, len(waveform))))))
        buffer_size = min(buffer_size, 8192)
        ljm.eWriteName(self.handle, f"{base}_TARGET", target_addr)
        ljm.eWriteName(self.handle, f"{base}_BUFFER_SIZE", buffer_size)
        ljm.eWriteName(self.handle, f"{base}_ENABLE", 1)
        ljm.eWriteName(self.handle, f"{base}_LOOP_SIZE", len(waveform))

        buffer_reg = f"{base}_BUFFER_F32" if value_type == "f32" else f"{base}_BUFFER_U16"
        for value in waveform:
            for attempt in range(3):
                try:
                    ljm.eWriteName(self.handle, buffer_reg, value)
                    break
                except ljm.LJMError:
                    if attempt == 2:
                        raise
                    time.sleep(0.01)

        if loop:
            ljm.eWriteName(self.handle, f"{base}_SET_LOOP", 1)

    # ------------------------------------------------------------------ streaming
    def start_stream(self):
        """Begin a hardware-timed stream for the configured scan list."""
        enabled_streams = [f"STREAM_OUT{STREAM_OUT_INDEX}"]
        if self._ttl_waveform:
            enabled_streams.append(f"STREAM_OUT{TTL_STREAM_OUT_INDEX}")

        scan_list_names = self.stream_config.build_scan_list(enabled_streams)
        self._scan_list_addresses = ljm.namesToAddresses(len(scan_list_names), scan_list_names)[0]
        self._num_inputs = len(self.stream_config.input_names)

        self.configure_led_stream_out()
        if self._ttl_waveform:
            self.configure_ttl_stream_out()

        self._actual_scan_rate = ljm.eStreamStart(
            self.handle,
            self.stream_config.scans_per_read,
            len(scan_list_names),
            self._scan_list_addresses,
            self.stream_config.scan_rate_hz,
        )
        return self._actual_scan_rate

    def read_stream_chunk(self) -> Tuple[List[float], float]:
        """
        Blocking call that returns one chunk of data plus the associated scan rate.

        Returns:
            (flat_buffer, actual_scan_rate_hz)
            flat_buffer layout: [AIN0_0, AIN1_0, AIN0_1, AIN1_1, ...]
        """
        data, device_backlog, ljm_backlog = ljm.eStreamRead(self.handle)
        # Matches LabJack's stream_basic_with_stream_out example: only input channels appear in data.
        return data, self._actual_scan_rate

    def stop_stream(self):
        """Stop streaming and disable the stream-out DAC."""
        self.disable_ttl_stream()
        ljm.eStreamStop(self.handle)
        base = f"STREAM_OUT{STREAM_OUT_INDEX}"
        ljm.eWriteName(self.handle, f"{base}_ENABLE", 0)
        ljm.eWriteName(self.handle, LED_DAC_CHANNEL, LED_IDLE_V)

    # ------------------------------------------------------------------ digital start pulse
    def fire_camera_start_pulse(self, width_s: float = START_PULSE_WIDTH_S):
        """Send a synchronous TTL pulse to the camera trigger line."""
        self._set_fio0_state(True)
        time.sleep(max(0.001, width_s))
        self._set_fio0_state(False)

    def disable_ttl_stream(self):
        """Disable the TTL stream-out channel and ensure the line returns low."""
        base = f"STREAM_OUT{TTL_STREAM_OUT_INDEX}"
        try:
            ljm.eWriteName(self.handle, f"{base}_ENABLE", 0)
        except ljm.LJMError:
            pass
        self._set_fio0_state(False)
        self._ttl_waveform = None
        self._ttl_duration_s = 0.0

    def _configure_fio0_direction(self):
        value = (FIO0_MASK_ALLOW << 8) | 0x01  # inhibit mask in upper byte, FIO0 output bit in lower byte
        self._write_optional_register("FIO_DIRECTION", value)  # set only FIO0 as a digital output

    def _set_fio0_state(self, high: bool):
        value = (FIO0_MASK_ALLOW << 8) | (0x01 if high else 0x00)  # same inhibit mask, but state bit depends on high flag
        self._write_optional_register("FIO_STATE", value)  # update FIO0 level without disturbing other FIOs

    def _write_optional_register(self, name: str, value: float):
        try:
            ljm.eWriteName(self.handle, name, value)  # attempt to write the given register/value pair
        except ljm.LJMError as exc:
            if getattr(exc, "errorCode", None) == errorcodes.MBE2_ILLEGAL_DATA_ADDRESS:
                return  # some devices/firmware don't expose this register; safe to ignore
            raise  # any other error should bubble up

    # ------------------------------------------------------------------ cleanup
    def close(self):
        try:
            self.stop_stream()
        except ljm.LJMError:
            pass
        ljm.close(self.handle)


def run_pulsed_stream(
    num_frames: int = NUM_CAMERA_FRAMES,
    trigger_rate_hz: float = CAMERA_TRIGGER_RATE_HZ,
    pulse_width_s: float = START_PULSE_WIDTH_S,
    post_run_padding_s: float = 0.1,
    log_path: Path | None = DATA_LOG_PATH,
):
    print(f"LabJack pulsing {num_frames} frames")
    """Helper that streams, emits a TTL pulse train, and optionally logs samples."""
    controller = StreamedLabJackController()
    controller.prepare_ttl_waveform(num_frames, trigger_rate_hz, pulse_width_s)
    controller.start_stream()

    writer = None
    csv_file = None
    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = log_path.open("w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["sample_index", "time_s", *controller.stream_config.input_names])

    num_inputs = len(controller.stream_config.input_names)
    
    scan_width = num_inputs  # per LJM docs, eStreamRead returns only input channels
    sample_index = 0
    sample_period = 1.0 / controller._actual_scan_rate if controller._actual_scan_rate else 0.0

    def log_chunk(data: List[float]):
        nonlocal sample_index
        if writer is None or not data:
            return
        # Trim data to the expected number of samples, for some reason extra samples at 0V appear.
        data = data[:SCANS_PER_READ * scan_width]
        scans = len(data) // scan_width
        for scan in range(scans):
            base = scan * scan_width
            values = [data[base + ch] for ch in range(num_inputs)]
            # print(values)
            writer.writerow([sample_index, sample_index * sample_period, *values])
            sample_index += 1

    try:
        ttl_end_time = time.time() + controller.ttl_duration_seconds
        while time.time() < ttl_end_time:
            data, _ = controller.read_stream_chunk()
            log_chunk(data)

        end_time = time.time() + post_run_padding_s
        while time.time() < end_time:
            data, _ = controller.read_stream_chunk()
            log_chunk(data)
    finally:
        if csv_file:
            csv_file.close()
        controller.stop_stream()
        controller.disable_ttl_stream()
        controller.close()


if __name__ == "__main__":
    run_pulsed_stream()
