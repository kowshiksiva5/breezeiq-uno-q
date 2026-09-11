#pragma once
#include <Arduino.h>
#include "BQAnalog.h"

/*
 * MQ-135 through the MANDATORY divider:
 *     AOUT --[10k]--+-- A1 --[15k]-- GND      5.0 V * 15/25 = 3.0 V worst case
 * The UNO Q is 3.3 V and not 5 V tolerant. AOUT sitting near the rail means the
 * divider is missing — that is the one wiring fault here that can kill a pin.
 *
 * Honest scope: this measures GASES, not particulates. It cannot produce the
 * PM2.5 AQI a phone app shows, and without a reference gas it cannot produce
 * ppm either. What it can do is hold a clean-air baseline (R0, the highest
 * resistance seen) and report how far the current reading sits from it.
 *
 * powf() is deliberately absent: it does NOT LINK on this core (newlib-nano
 * here has no __errno). Band thresholds are precomputed from the power-law
 * curve instead, and any ppm conversion is done host-side.
 */
namespace bq {

class Mq135 {
public:
  explicit Mq135(int pin) : _pin(pin) {}

  void update() {
    _s = readAnalog(_pin);
    if (_s.value <= 15 || _s.value >= ADC_MAX - 15) return;
    _rsK = rsFromAdc(_s.value);
    if (isnan(_r0K) || _rsK > _r0K) _r0K = _rsK;   // cleanest air seen
  }

  int   raw()  const { return _s.value; }
  int   span() const { return _s.span; }
  float rsK()  const { return _rsK; }
  float r0K()  const { return _r0K; }
  bool  hasBaseline() const { return !isnan(_r0K); }
  bool  dividerMissing() const { return _s.value > ADC_MAX * 0.93; }
  float ratio() const { return (isnan(_r0K) || _r0K <= 0) ? NAN : _rsK / _r0K; }

  // Thresholds precomputed from ppm = 400 * ratio^-2.862 against the usual
  // indoor CO2-equivalent bands. Comparisons, so no libm call is needed.
  const char *band() const {
    float r = ratio();
    if (isnan(r)) return "no baseline";
    return r > 0.868f ? "Good"
         : r > 0.726f ? "Moderate"
         : r > 0.630f ? "Poor"
         : r > 0.527f ? "Very poor" : "Severe";
  }

  void setBaselineNow() { if (_s.value > 15) _r0K = rsFromAdc(_s.value); }
  void reset() { _r0K = NAN; }

private:
  float rsFromAdc(int adc) const {
    float vA1  = (adc / float(ADC_MAX)) * VREF;
    float vOut = vA1 / DIVIDER;                       // undo the divider
    vOut = vOut > SUPPLY - 0.01f ? SUPPLY - 0.01f : (vOut < 0.01f ? 0.01f : vOut);
    return (SUPPLY - vOut) / vOut * RL_K;             // Rs
  }
  static constexpr float DIVIDER = 0.6f;    // 15k / (10k + 15k)
  static constexpr float VREF    = 3.3f;
  static constexpr float SUPPLY  = 5.0f;
  static constexpr float RL_K    = 10.0f;   // load resistor on the breakout

  int _pin;
  AnalogSample _s;
  float _rsK = NAN, _r0K = NAN;
};

}  // namespace bq
