#pragma once
#include <Arduino.h>

/*
 * THE pin map. One definition, imported by every sketch.
 *
 * This file exists because the pins drifted: hwcheck moved the outdoor DHT to
 * "4" while t1_dht still said "3", so a test sketch reported a working sensor
 * as FAIL. Names below are exactly as silkscreened on the header — no "D"
 * prefix, "~" kept, so the code and the board read the same.
 */
namespace bq {
namespace pins {

constexpr int DHT_INDOOR  = 7;    // "7"
constexpr int DHT_OUTDOOR = 4;    // "4"
constexpr int PIR         = 8;    // "8"
constexpr int LDR         = A0;   // "A0"   3.3V-[LDR]-A0-[10k]-GND
constexpr int MQ135       = A1;   // "A1"   AOUT-[10k]-A1-[15k]-GND, mandatory
constexpr int SERVO_BLIND = 5;    // "5~"   rail B, never the board's 5V pin
constexpr int SERVO_WINDOW = 6;   // "6~"   rail B

}  // namespace pins
}  // namespace bq
