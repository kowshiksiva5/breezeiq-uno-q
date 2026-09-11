#pragma once
/*
 * BreezeIQ shared sensor modules.
 *
 * One implementation per sensor, used by the t1..t5 calibration sketches only.
 * The product sketch (`breezeiq/breezeiq.ino`) does not include this header —
 * it carries its own sensor code. Before this existed each calibration sketch
 * carried its own copy and they drifted; a fix in one did not reach the
 * others, which is how a stale bug survives a "tested" claim.
 *
 * Every quirk below was measured on this board, not read off a datasheet:
 *   - <Servo.h> + <DHT.h> cannot coexist        -> BQServo uses raw pulses
 *   - powf() does not link (no __errno)         -> BQMq135 precomputes bands
 *   - analogRead() drops the internal pull-up   -> BQAnalog never relies on it
 *   - a bare ADC pad reads plausibly            -> compare against A2-A5
 */
#include "BQPins.h"
#include "BQAnalog.h"
#include "BQDht.h"
#include "BQLdr.h"
#include "BQPir.h"
#include "BQMq135.h"
#include "BQServo.h"
