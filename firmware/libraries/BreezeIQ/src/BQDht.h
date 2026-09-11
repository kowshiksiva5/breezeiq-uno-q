#pragma once
#include <Arduino.h>
#include <DHT.h>

/*
 * DHT22 wrapper: the stock Adafruit library plus the three things a bring-up
 * actually needs — a per-unit offset, a read success rate, and an idle noise
 * band.
 *
 * Do NOT reimplement the protocol. This project bit-banged it once, lost hours
 * to four self-inflicted bugs, and the library worked first try on this core.
 */
namespace bq {

class Dht {
public:
  Dht(int pin, uint8_t type = DHT22) : _dht(pin, type), _pin(pin) {}

  void begin() { _dht.begin(); }

  // The library rate-limits internally (a DHT22 samples at 0.5 Hz) and returns
  // its cached frame in between, so calling this once a second is fine.
  bool read() {
    _tries++;
    float t = _dht.readTemperature();
    float h = _dht.readHumidity();
    if (isnan(t) || isnan(h)) return false;
    _rawC = t;
    _c    = t + offsetC;
    _rh   = h + offsetRh;
    _ok++;
    if (_c < _minC) _minC = _c;
    if (_c > _maxC) _maxC = _c;
    return true;
  }

  float offsetC = 0.0f, offsetRh = 0.0f;   // per-unit correction, see calibrateTo()

  float tempC()  const { return _c; }
  float rawC()   const { return _rawC; }
  float rh()     const { return _rh; }
  bool  alive()  const { return _ok > 0; }
  int   okPct()  const { return _tries ? (100 * _ok) / _tries : 0; }
  float noiseC() const { return _ok ? _maxC - _minC : 0.0f; }
  int   pin()    const { return _pin; }

  // Make this unit agree with a reference unit sitting on the same desk.
  // Absolute accuracy needs a calibrated thermometer; AGREEMENT is what the
  // two-sensor fault detector actually depends on.
  void calibrateTo(float referenceC) {
    if (isnan(_rawC)) return;
    offsetC += referenceC - _c;
    resetStats();
  }

  void resetStats() { _ok = _tries = 0; _minC = 1e9f; _maxC = -1e9f; }

private:
  DHT   _dht;
  int   _pin;
  float _c = NAN, _rawC = NAN, _rh = NAN;
  float _minC = 1e9f, _maxC = -1e9f;
  int   _ok = 0, _tries = 0;
};

}  // namespace bq
