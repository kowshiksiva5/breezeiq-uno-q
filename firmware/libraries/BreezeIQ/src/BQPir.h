#pragma once
#include <Arduino.h>

/*
 * HC-SR501. A PIR is an EVENT source, not a level, and two things must be
 * measured before writing occupancy logic on top of it:
 *   1. the retrigger pulse width (the Tx pot) — sets how often you must poll
 *   2. the false-trigger rate in an empty room
 *
 * Warm-up is enforced: the sensor lies for the first 30-60 s after power-up,
 * and bring-up done inside that window is where "my PIR is broken" comes from.
 *
 * Note it CANNOT self-verify presence. A bare CMOS input picks up enough noise
 * to fake any single level, so "is a PIR attached" is settled by a human waving
 * at it, not by the firmware. Three detection heuristics were tried and failed.
 */
namespace bq {

class Pir {
public:
  explicit Pir(int pin, unsigned long warmupMs = 45000)
    : _pin(pin), _warmupMs(warmupMs) {}

  void begin() { pinMode(_pin, INPUT); _bootAt = millis(); }

  void update() {
    bool now = digitalRead(_pin) == HIGH;
    if (now && !_prev) {
      _highSince = millis();
      if (warm() && millis() - _lastEventAt > _debounceMs) {
        _lastEventAt = millis();
        _events++;
      }
    }
    if (!now && _prev && _highSince) {
      _lastPulseMs = millis() - _highSince;
      if (_lastPulseMs < _minPulseMs) _minPulseMs = _lastPulseMs;
      if (_lastPulseMs > _maxPulseMs) _maxPulseMs = _lastPulseMs;
    }
    _prev = now;
  }

  bool warm() const { return millis() - _bootAt >= _warmupMs; }
  unsigned long warmupLeftMs() const {
    unsigned long e = millis() - _bootAt;
    return e >= _warmupMs ? 0 : _warmupMs - e;
  }
  bool high()  const { return _prev; }
  unsigned long events()      const { return _events; }
  unsigned long lastPulseMs() const { return _lastPulseMs; }
  unsigned long minPulseMs()  const { return _minPulseMs == 0xFFFFFFFF ? 0 : _minPulseMs; }
  unsigned long maxPulseMs()  const { return _maxPulseMs; }
  unsigned long quietMs()     const { return millis() - _lastEventAt; }
  // Over ~6 s of hold makes the poll rate awkward; turn the Tx pot anticlockwise.
  bool pulseTooLong() const { return _lastPulseMs > 6000; }

  void reset() { _events = 0; _lastPulseMs = 0; _minPulseMs = 0xFFFFFFFF; _maxPulseMs = 0; }

private:
  int _pin;
  unsigned long _warmupMs, _bootAt = 0;
  unsigned long _highSince = 0, _lastEventAt = 0, _events = 0;
  unsigned long _lastPulseMs = 0, _minPulseMs = 0xFFFFFFFF, _maxPulseMs = 0;
  unsigned long _debounceMs = 300;
  bool _prev = false;
};

}  // namespace bq
