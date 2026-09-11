#pragma once
#include <Arduino.h>

/*
 * Stable analogue reads. Every ADC channel on this rig goes through here so
 * "how do we sample" is answered in exactly one place.
 *
 * Median, not mean: one mains-hum spike drags a mean but not a median.
 * The span (max-min across the burst) is returned too, because a settled
 * divider is quiet and a loose or half-built one is not — that spread is the
 * single most useful wiring diagnostic on an analogue pin.
 */
namespace bq {

constexpr int ADC_MAX = 4095;          // 12-bit; call analogReadResolution(12)

struct AnalogSample {
  int value = 0;                       // median of the burst
  int span  = 0;                       // max-min: noise / instability
  bool stable(int limit = 300) const { return span < limit; }
};

inline AnalogSample readAnalog(int pin, int samples = 8, int gapMs = 2) {
  if (samples < 3) samples = 3;
  if (samples > 16) samples = 16;
  int v[16];
  for (int i = 0; i < samples; i++) { v[i] = analogRead(pin); delay(gapMs); }
  for (int i = 1; i < samples; i++)
    for (int j = i; j > 0 && v[j] < v[j-1]; j--) { int t = v[j]; v[j] = v[j-1]; v[j-1] = t; }
  AnalogSample s;
  s.value = v[samples / 2];
  s.span  = v[samples - 1] - v[0];
  return s;
}

// A2-A5 carry nothing on this rig and never will, so they are the control
// group: whatever a bare pad reads, they read it too. A channel sitting inside
// their band is indistinguishable from disconnected. This is a measurement
// against a reference, not a heuristic — three heuristics were tried first and
// a floating CMOS input defeated all of them.
inline bool insideFloatingBand(int value, int refLo, int refHi, int margin = 120) {
  return value >= refLo - margin && value <= refHi + margin;
}

}  // namespace bq
