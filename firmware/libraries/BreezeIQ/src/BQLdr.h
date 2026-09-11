#pragma once
#include <Arduino.h>
#include "BQAnalog.h"

/*
 * LDR on a divider:  3.3V --[LDR]--+-- A0 --[10k]-- GND
 *
 * The resistor is not optional. An LDR is only a variable resistance; alone it
 * produces no voltage for the ADC to read. Without it the node is high-impedance
 * and gets WORSE in the dark, because the LDR climbs to ~1 MOhm and the pin is
 * then driven by nothing at all.
 *
 * This class IS the calibration procedure: it captures the darkest and brightest
 * settled readings it has seen and hands back the two constants the controller
 * needs. No lux meter — every LDR differs and the ladder only threshold-compares.
 */
namespace bq {

class Ldr {
public:
  explicit Ldr(int pin) : _pin(pin) {}

  void update() {
    _s = readAnalog(_pin);
    // Only extend the calibration from SETTLED samples. A floating or half-built
    // divider would otherwise pin dark at 0 and sun at 4095 and the captured
    // constants would be pure noise.
    if (_s.stable(_stableLimit) && _s.value > 15 && _s.value < ADC_MAX - 15) {
      if (_s.value < _dark) _dark = _s.value;
      if (_s.value > _sun)  _sun  = _s.value;
    }
  }

  int   raw()   const { return _s.value; }
  int   span()  const { return _s.span; }
  bool  stable() const { return _s.stable(_stableLimit); }
  int   dark()  const { return _dark == ADC_MAX ? -1 : _dark; }
  int   sun()   const { return _sun; }
  int   range() const { return (_dark == ADC_MAX) ? 0 : _sun - _dark; }
  bool  calibrated() const { return range() > 500; }

  // The controller wants a 0..1 solar index, not lux.
  float index() const {
    if (range() <= 0) return 0.0f;
    float i = float(_s.value - _dark) / float(_sun - _dark);
    return i < 0 ? 0 : (i > 1 ? 1 : i);
  }

  void resetCalibration() { _dark = ADC_MAX; _sun = 0; }

private:
  int _pin;
  int _dark = ADC_MAX, _sun = 0;
  int _stableLimit = 300;
  AnalogSample _s;
};

}  // namespace bq
