# Five-channel touch acquisition

Stream thumb, index, middle, ring, and little-finger ADC readings to the hand
application over USB.

The sketch targets a **Blue Pill STM32F103C8**, with analog channels **PA0–PA4**
in that order. It uses 12-bit readings, eight-sample averaging, and a target
100 Hz CSV stream at 115200 baud.

With PlatformIO installed and an ST-Link connected to the sensor board, run
these commands from this directory:

```bash
pio run
pio run --target upload
```

See [Sensor setup](../docs/sensors.md) for electrical requirements, the data
format, channel checks, and unloaded zeroing. Grip regulation runs in the
artificial brain; this sketch supplies the observations.

## License

The acquisition example is covered by the [project license](../LICENSE).
The board, Arduino framework, and programming tools retain their own terms.
