# Set up five touch sensors

Give each fingertip a contact channel and stream the five readings over a
separate USB connection. The acquisition board measures touch; Prima-Lux runs
the neural pathways that regulate closing.

## Hardware and channel order

The included sketch uses a **Blue Pill STM32F103C8**, the Arduino framework, and
these analog pins:

| Finger | Pin |
| --- | --- |
| Thumb | PA0 |
| Index | PA1 |
| Middle | PA2 |
| Ring | PA3 |
| Little | PA4 |

Use five mounted analog contact sensors, suitable signal-conditioning circuitry,
and a data USB cable. Select the sensor model, resistor network, and conditioning
for your hardware; those components are not specified by the acquisition sketch.
Check the sensor and board documentation for voltage limits before wiring the
analog inputs. Keep the sensor cables clear of the moving mechanism.

Another USB board can be used with the same data format. Adapt the pin assignments
and ADC setup to that board and select its port explicitly.

## Data format

The board sends this header followed by complete six-integer CSV lines:

```csv
time_ms,thumb,index,middle,ring,little
1000,100,120,110,130,105
1010,105,121,111,134,107
```

`time_ms` is the board's unsigned millisecond counter. The five channel values
are raw ADC readings from **0 to 4095**.

The supplied sketch requests 12-bit conversion, averages eight reads per channel,
and schedules samples every 10,000 microseconds, targeting **100 Hz**. Its USB
serial setting is **115200 baud**. The application drains the stream continuously
and keeps the newest complete reading. Device timestamps and host receipt times
are recorded separately.

## Build the acquisition firmware

Install PlatformIO and connect an ST-Link to the sensor board, then run:

```bash
cd sensor_firmware
pio run
pio run --target upload
cd ..
```

These commands program the **sensor board**, not the hand controller or Prima-Lux.
A board already streaming the supported format is ready to use without flashing.

## Read the sensors

From the repository root with `.venv` active:

```bash
python -m serial.tools.list_ports -v
export TACTILE_PORT="/dev/ttyACM0"
python run.py --read-tactile --tactile-port "$TACTILE_PORT"
```

Use your sensor board's actual port. `--tactile-port auto` can select a uniquely
identified STM32/Maple board. The sensor board and robotic hand need different
ports; each reader uses the connection settings for its own device.

Press one pad at a time and confirm that the corresponding channel changes.
In `config.local.json`, set each `tactile_directions` entry to `1` for increasing
readings under contact or `-1` for decreasing readings. The order is thumb,
index, middle, ring, little.

## Set the unloaded reference

Live startup opens the hand and captures unloaded offsets and idle noise. Leave
the pads clear during this step. To refresh the zero, select **Hold** and then
**Zero unloaded sensors** in the UI.

The captured baseline stays fixed through a grasp. The UI shows raw readings
and normalized inputs, so you can see both the sensor's offset and the contact
signal sent to the brain. The scale represents contact activity, not newtons
or a target grip force. A stale reading is treated as missing information,
not as zero contact.

Follow [the neural-grip guide](neural-grip.md) to see how each touch input
influences its motor pathways.
