
#include <Arduino.h>

constexpr int SENSOR_COUNT = 5;
constexpr int SENSOR_PINS[SENSOR_COUNT] = {
    PA0, PA1, PA2, PA3, PA4
};
constexpr int N_AVERAGE = 8;
constexpr uint32_t SAMPLE_PERIOD_US = 10000; // 100 Hz target

void setup() {
    SerialUSB.begin(115200);
    analogReadResolution(12);
    delay(1500);

    SerialUSB.println("time_ms,thumb,index,middle,ring,little");
}

void loop() {
    static uint32_t nextSample = micros();

    if ((int32_t)(micros() - nextSample) < 0) {
        return;
    }
    nextSample += SAMPLE_PERIOD_US;

    uint16_t values[SENSOR_COUNT];

    for (int s = 0; s < SENSOR_COUNT; s++) {
        uint32_t sum = 0;

        for (int i = 0; i < N_AVERAGE; i++) {
            sum += analogRead(SENSOR_PINS[s]);
        }

        values[s] = (sum + N_AVERAGE / 2) / N_AVERAGE;
    }

    SerialUSB.print(millis());

    for (int s = 0; s < SENSOR_COUNT; s++) {
        SerialUSB.print(",");
        SerialUSB.print(values[s]);
    }

    SerialUSB.println();
}
