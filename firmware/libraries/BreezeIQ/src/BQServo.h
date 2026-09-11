#pragma once
#include <Arduino.h>

/*
 * SG90 driven with raw 50 Hz pulses, NOT the Servo library.
 *
 * MEASURED 2026-08-09 on arduino:zephyr: linking <Servo.h> alongside <DHT.h>
 * makes readTemperature() return NaN forever and slows the loop to ~7 s per
 * pass. It fails even with attach() removed, so it is the linkage rather than
 * the call — they contend for a timer. Servo was tried first, as it should be.
 *
 * digitalWrite costs 408 ns on this core (measured), so a 1000-2000 us pulse is
 * comfortably accurate. Detach between moves: a parked servo that keeps holding
 * draws current and buzzes for no benefit.
 */
namespace bq {

class Servo {
public:
  explicit Servo(int pin) : _pin(pin) {}

  // Widen these until the horn stops moving further, then back off one step —
  // that is the real endpoint, not the datasheet's.
  int minUs = 900, maxUs = 2100;

  void begin() { pinMode(_pin, OUTPUT); digitalWrite(_pin, LOW); }

  void write(int deg, int holdMs = 300) {
    _deg = deg < 0 ? 0 : (deg > 180 ? 180 : deg);
    int us = minUs + (long)(maxUs - minUs) * _deg / 180;
    pulseFor(us, holdMs);
    digitalWrite(_pin, LOW);          // release
  }

  // The servo is the only load big enough to collapse a weak 5 V rail, so a
  // sweep doubles as the rail-B stress test. Watch the other channels across it.
  void sweep(int fromDeg = 10, int toDeg = 170, int stepDeg = 10) {
    for (int a = fromDeg; a <= toDeg; a += stepDeg) write(a, 60);
    for (int a = toDeg; a >= fromDeg; a -= stepDeg) write(a, 60);
    write(90, 200);
  }

  int angle() const { return _deg; }

private:
  void pulseFor(int us, int ms) {
    for (int elapsed = 0; elapsed < ms; elapsed += 20) {
      digitalWrite(_pin, HIGH);
      delayMicroseconds(us);
      digitalWrite(_pin, LOW);
      delayMicroseconds(20000 - us);
    }
  }
  int _pin, _deg = 90;
};

}  // namespace bq
