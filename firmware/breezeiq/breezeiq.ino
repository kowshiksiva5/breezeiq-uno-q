/*
 * BreezeIQ wiring verifier. One question per input: is it connected, and if
 * not, what is wrong. Emits a key=value line per second for the dashboard and
 * lights one matrix column per channel that produced a usable reading:
 *
 *   col 0 = DHT22 indoor   col 1 = DHT22 outdoor   col 2 = LDR
 *   col 3 = PIR            col 4 = MQ-135
 *
 * It also holds the one promise Linux cannot keep for itself: if the Linux
 * side stops calling over the Bridge, this sketch parks the blinds motor in
 * its safe position on its own. See the failsafe section below.
 *
 * Four RPCs are exposed: breezeiq/read (telemetry), breezeiq/cmd (motor and
 * the display), breezeiq/display (the display alone), and breezeiq/radar_tune
 * (a bench-only hex relay onto the radar's serial port; the deployed loop never
 * calls it). Every one of them is also proof Linux is alive, so every one of
 * them re-arms the failsafe -- as does every complete serial command line.
 */

#include <DHT.h>
#include <Wire.h>
#include "Arduino_LED_Matrix.h"
#include "Arduino_RouterBridge.h"

// Adafruit DHT 1.4.7 calls these Arduino API functions from its timing guard,
// but Arduino UNO Q Zephyr core 0.90.0 declares them without exporting an
// implementation to App Lab sketches. Interrupt masking also stalls the UNO Q
// bridge transport. Keep the library's proven pulse decoder and map its guard
// to Zephyr's scheduler lock. This prevents thread preemption during the pulse
// train while leaving RouterBridge and other hardware interrupts operational.
#if defined(ARDUINO_ARCH_ZEPHYR)
extern "C" void k_sched_lock(void);
extern "C" void k_sched_unlock(void);
void noInterrupts() { k_sched_lock(); }
void interrupts() { k_sched_unlock(); }
#endif

constexpr int PIN_DHT_INDOOR  = 7;    // "7"
constexpr int PIN_DHT_OUTDOOR = 4;    // "4"
constexpr int PIN_PIR         = 8;    // "8"
constexpr int PIN_LDR         = A0;   // "A0"
constexpr int PIN_MQ135       = A1;   // "A1"
constexpr int PIN_RADAR       = 2;    // "2"  LD2410C OUT, 3.3 V logic -- back to its physical
                                       // wiring. The D2->D13 move was a diagnostic (isolating
                                       // pin vs. sensor); the sensor itself is the dead part
                                       // either way -- see the README's wiring table. Kept wired and read
                                       // rather than removed: it's still physically on the rig.

// BH1750 on the I²C header pins, which on this board means Wire2 and not Wire.
// The variant declares i2cs = <&i2c2>, <&i2c4>, <&i2c3>, so Wire is i2c2 on
// PB10/PB11 (broken out as D21/D20), Wire1 is i2c4 (the camera's bus, left
// alone), and only Wire2 is i2c3 on PC0/PC1 — the pads silkscreened SCL and
// SDA, which are also A5 and A4. Scanning Wire finds an empty bus and looks
// exactly like a dead sensor, which cost an evening; the bus is named once
// here so it cannot be assumed again.
//
// Register-level because the whole part is three bytes: a mode, a wait, and a
// big-endian count that is lux times 1.2. Both addresses are probed at run
// time rather than one being assumed — see bh1750Begin().
// All three, because "which bus" and "is it wired" are different questions and
// only a scan of every bus separates them. Wire is i2c2 (PB10/PB11 = D21/D20),
// Wire1 is i2c4, Wire2 is i2c3 (PC0/PC1 = the SCL/SDA pads, also A5/A4).
arduino::ZephyrI2C *const I2C_BUSES[] = {&Wire, &Wire1, &Wire2};
constexpr int I2C_BUS_COUNT = sizeof(I2C_BUSES) / sizeof(I2C_BUSES[0]);
constexpr uint8_t  BH1750_ADDR_LOW   = 0x23;   // ADDR tied low / floating
constexpr uint8_t  BH1750_ADDR_HIGH  = 0x5C;   // ADDR tied to 3.3 V
constexpr uint8_t  BH1750_CONT_HIGH = 0x10;   // continuous, 1 lx, ~120 ms
constexpr float    BH1750_DIVISOR   = 1.2f;
constexpr float    BH1750_MAX_LUX   = 54612.0f;

constexpr int ADC_MAX = 4095;         // 12-bit
// How far off either rail a count has to sit before it is worth anything. The
// host's reader uses the same 15; keep them equal or the two disagree about
// which channels are alive.
constexpr int ADC_RAIL_MARGIN = 15;

constexpr unsigned long SAMPLE_PERIOD_MS = 1000;
unsigned long nextSampleAt = 0;

// ── live tuning state ───────────────────────────────────────────────────
// hwcheck feeds the dashboard, so the tuning numbers you would otherwise get
// from t1..t5 are computed here too. Cover the LDR / wave at the PIR and the
// constants appear on the page — no reflashing between tune and observe.
int  ldrMin = ADC_MAX, ldrMax = 0;         // -> LDR_RAW_DARK / LDR_RAW_SUN
int  dhtOk = 0, dhtTry = 0;                // DHT read success rate
float dhtMinC = 1e9, dhtMaxC = -1e9;       // DHT noise band when idle
unsigned long pirEvents = 0, pirHighSince = 0, pirLastPulse = 0;
bool  pirPrev = false;
float mqR0k = NAN;                         // clean-air baseline

// Calibrated 2026-08-09 against a trusted 25.0 C room reference after the
// firmware restart. Twelve live samples averaged 27.06 C indoors and 27.00 C
// outdoors. Keep the raw values in telemetry as proof of the sensor output.
constexpr float TEMP_REFERENCE_C  = 25.0f;
constexpr float TEMP_OFFSET_IN_C  = -2.1f;
constexpr float TEMP_OFFSET_OUT_C = -2.0f;

DHT dhtIndoor(PIN_DHT_INDOOR, DHT22);
DHT dhtOutdoor(PIN_DHT_OUTDOOR, DHT22);
ArduinoLEDMatrix matrix;
uint8_t frame[8 * 13];
String latestSnapshot = "";

enum Status : uint8_t { ST_OK, ST_NO_RESPONSE };

// Use the underlying byte in this public signature because App Lab's Arduino
// preprocessor injects function prototypes above sketch-defined enum types.
const char *statusName(uint8_t s) {
  switch (s) {
    case ST_OK:          return "OK";
    case ST_NO_RESPONSE: return "NO_RESPONSE";
    default:             return "NO_RESPONSE";
  }
}

// No connectivity heuristic here any more. Three of them were tried (internal
// pull-up/pull-down, absolute level, single-HIGH latch) and a floating CMOS
// input defeated all three — the old code reported a confident OK on a pin with
// nothing attached, and the dashboard turned that into a fake air-quality
// reading. The board now reports the raw count and nothing else; deciding what
// is connected is done host-side by comparing against A0, A2 and A3, which are
// known to have nothing on them. A4/A5 used to be in that set and no longer
// qualify: they are the I²C bus now, and a driven pin is not a reference.
void readAnalogStable(int pin, int &average, int &span) {
  // Discard the first conversion after switching ADC channels, then measure a
  // short burst. A driven divider stays tight; a loose/floating node wanders.
  (void)analogRead(pin);
  delayMicroseconds(200);
  long total = 0;
  int low = ADC_MAX, high = 0;
  for (int i = 0; i < 8; i++) {
    int value = analogRead(pin);
    total += value;
    if (value < low) low = value;
    if (value > high) high = value;
    delayMicroseconds(100);
  }
  average = (int)(total / 8);
  span = high - low;
}

// The PIR reports its raw level. Whether that level means "a PIR is attached"
// is not something this board can decide — see the note above.
bool readPir(int pin) {
  pinMode(pin, INPUT);
  return digitalRead(pin) == HIGH;
}

// The radar's OUT is a level, exactly like the PIR's, and carries the same
// caveat: this board reports what the pin says and does not claim a part is
// attached. What differs is upstream — OUT stays HIGH for a person sitting
// still, which is the whole reason it is here beside a PIR that will not.
//
// The level alone is not enough to tell a working radar from a dead one: a
// stuck pin and a genuinely occupied room read identically at any single
// instant. What separates them is TIME — a live sensor changes its mind
// eventually, a dead one never does. So the two figures below ride along
// with the level, and they are what actually diagnosed this part: edges
// counts transitions since boot, held_ms is how long the current level has
// stood. Zero edges over hours is not presence, it is a fault.
unsigned long radarEdges = 0;
unsigned long radarHeldSince = 0;
int radarLastLevel = -1;

bool readRadar(int pin) {
  pinMode(pin, INPUT);
  bool level = digitalRead(pin) == HIGH;
  if (radarLastLevel != (int)level) {
    if (radarLastLevel != -1) radarEdges++;   // boot is not an edge
    radarLastLevel = (int)level;
    radarHeldSince = millis();
  }
  return level;
}

// Which address answered, or 0. Kept as state because the useful question is
// not "did this read fail" but "is the part on the bus at all", and those need
// different fixes — a swapped SDA/SCL pair versus a floating ADDR pin.
uint8_t bh1750Addr = 0;
int     bh1750Bus  = -1;
int     i2cDevices = -1;
int     busDevices[3] = {-1, -1, -1};
bool    sdaPullup = false, sclPullup = false;   // boot-time only, see setup()
int     sdaLevel = -1, sclLevel = -1;           // the same pins via the ADC

// Probes with a one-byte read, not an empty write. endTransmission() with
// nothing buffered lowers to i2c_write(len=0), which this STM32 driver refuses
// outright — so the textbook Arduino scan reports an empty bus whatever is on
// it. A read puts a real START and address on the wire and the ACK is the
// answer. Non-destructive: nothing is written to a part we have not identified.
bool i2cPresent(arduino::ZephyrI2C *bus, uint8_t addr) {
  return bus->requestFrom((int)addr, 1) == 1;
}

// Try both BH1750 addresses rather than only the wired one. ADDR floating
// instead of tied is the single most likely wiring slip on this part, and it
// presents as total silence at 0x23 while the sensor sits happily at 0x5C.
// Re-runs while unfound, so plugging it in does not need a reboot.
void bh1750Begin() {
  bh1750Addr = 0;
  bh1750Bus = -1;
  for (int b = 0; b < I2C_BUS_COUNT && !bh1750Addr; b++) {
    if (i2cPresent(I2C_BUSES[b], BH1750_ADDR_LOW))       bh1750Addr = BH1750_ADDR_LOW;
    else if (i2cPresent(I2C_BUSES[b], BH1750_ADDR_HIGH)) bh1750Addr = BH1750_ADDR_HIGH;
    if (bh1750Addr) bh1750Bus = b;
  }
  if (bh1750Bus < 0) return;
  // A one-byte write, which is a real transfer and therefore actually lands.
  arduino::ZephyrI2C *bus = I2C_BUSES[bh1750Bus];
  bus->beginTransmission(bh1750Addr);
  bus->write(BH1750_CONT_HIGH);
  if (bus->endTransmission() != 0) { bh1750Addr = 0; bh1750Bus = -1; }
}

// Counts everything that ACKs. Distinguishes "the bus is dead" from "the bus
// works and this part is not on it" — one is wiring to the board, the other is
// wiring to the module, and guessing between them wastes an evening.
int i2cScan() {
  int found = 0;
  for (int b = 0; b < I2C_BUS_COUNT; b++) {
    busDevices[b] = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++)
      if (i2cPresent(I2C_BUSES[b], addr)) busDevices[b]++;
    found += busDevices[b];
  }
  return found;
}

// Negative means "no answer", which is a different fact from 0 lux and has to
// survive as one — a dark room and an absent sensor must never serialise the
// same. An I²C part either ACKs or it does not, so unlike the ADC channels
// this one can honestly report its own presence.
//
// Every OTHER channel on this board is a burst average — readAnalogStable()
// takes 8 samples specifically because "a driven divider stays tight; a
// loose/floating node wanders." The BH1750's first live minutes proved the
// same is true of it: lux swung 24 to 127 tick to tick on a room the LDR saw
// as perfectly steady throughout, which is enough on its own to flap the fused
// dark/normal verdict.
//
// Smoothed across TICKS rather than as a burst inside one. A burst needs the
// part's ~120 ms conversion time between samples to get fresh conversions
// rather than the same register three times, and spending 60+ ms blocked here
// would starve serviceIo(), which has to keep pumping the motor's run timer
// and the command channel — the sensor read sits mid-tick, where a timed
// drive may be in progress. The
// sampler already runs at 1 Hz, so a short running mean over consecutive
// ticks costs nothing and blocks nobody.
//
// A failed transfer resets the window instead of averaging across the gap: a
// part that dropped off the bus and came back is not the same reading it was
// before, and a missing sensor must stay unambiguous.
constexpr int BH1750_WINDOW = 4;
float bh1750Recent[BH1750_WINDOW];
int   bh1750Count = 0;

float readBh1750() {
  if (!bh1750Addr || bh1750Bus < 0) { bh1750Count = 0; return -1.0f; }
  arduino::ZephyrI2C *bus = I2C_BUSES[bh1750Bus];
  if (bus->requestFrom((int)bh1750Addr, 2) != 2) {
    bh1750Addr = 0; bh1750Count = 0; return -1.0f;
  }
  uint16_t raw = ((uint16_t)bus->read() << 8) | bus->read();
  float lux = raw / BH1750_DIVISOR;
  if (lux > BH1750_MAX_LUX) lux = BH1750_MAX_LUX;

  if (bh1750Count < BH1750_WINDOW) {
    bh1750Recent[bh1750Count++] = lux;
  } else {
    for (int i = 1; i < BH1750_WINDOW; i++) bh1750Recent[i - 1] = bh1750Recent[i];
    bh1750Recent[BH1750_WINDOW - 1] = lux;
  }
  float total = 0;
  for (int i = 0; i < bh1750Count; i++) total += bh1750Recent[i];
  return total / bh1750Count;
}

// Three tenants on one 8x13 panel, and they do not overlap by construction:
//
//   rows 1-6, x = 1,3,5,7,9   five sensor-health columns (this board's own)
//   row  7,   x = 0..8        people count, one pixel each (from Linux)
//   rows 0-7, x = 10          occupancy mode glyph        (from Linux)
//   rows 0-7, x = 11..12      failsafe                    (this board's own)
constexpr int MATRIX_COLS = 13;
constexpr int MATRIX_ROWS = 8;
constexpr int FAILSAFE_GLYPH_COL = 11;   // 11..12, clear of every other tenant
constexpr int OCCUPANCY_GLYPH_COL = 10;
constexpr int PEOPLE_BAR_ROW = 7;
constexpr int PEOPLE_MAX = 9;
constexpr int OCCUPANCY_MODES = 4;

// One bit per row, LSB = row 0. Four shapes nobody has to decode: nothing, a
// dot, a solid column, a dashed column.
constexpr uint8_t OCCUPANCY_GLYPH[OCCUPANCY_MODES] = {
  0x00,   // 0 EMPTY    — a dark column, because nobody is here
  0x18,   // 1 OCCUPIED — a dot at the middle
  0xFF,   // 2 CROWDED  — solid
  0x55,   // 3 ASLEEP   — dashed
};

bool channelOk[5] = {false, false, false, false, false};
bool failsafeActive = false;
int  occupancyMode = -1;      // -1 = Linux has not said yet, so nothing drawn
int  peopleCount = 0;

// File-scope inputs rather than parameters so the failsafe and the display RPC
// can repaint the panel the instant they change something, instead of waiting
// for the next 1 Hz sample.
void drawMatrix() {
  memset(frame, 0, sizeof frame);
  for (int ch = 0; ch < 5; ch++)
    if (channelOk[ch])
      for (int row = 1; row < 7; row++)
        frame[row * MATRIX_COLS + ch * 2 + 1] = 200;

  // Occupancy is Linux's fact, not this board's. A failsafe episode means the
  // process that produced it has gone silent, so the glyph comes down with it
  // — a stale mode left glowing would be a claim the MCU cannot support.
  if (!failsafeActive && occupancyMode >= 0) {
    uint8_t bits = OCCUPANCY_GLYPH[occupancyMode];
    for (int row = 0; row < MATRIX_ROWS; row++)
      if (bits & (1 << row))
        frame[row * MATRIX_COLS + OCCUPANCY_GLYPH_COL] = 220;
    for (int i = 0; i < peopleCount && i < PEOPLE_MAX; i++)
      frame[PEOPLE_BAR_ROW * MATRIX_COLS + i] = 160;
  }

  if (failsafeActive)
    for (int row = 0; row < MATRIX_ROWS; row++)
      for (int col = FAILSAFE_GLYPH_COL; col < MATRIX_COLS; col++)
        frame[row * MATRIX_COLS + col] = 255;
  matrix.draw(frame);
}


// ── curtain/blinds motor, driven from Linux ──────────────────────────────
// One motor (RS555SF/3162, 12 V-rated, run from the 9 V rail C) through a
// bare L298 driver does the curtain/blinds job alone.
//
// SRV1 is a wire-protocol name, not a claim about hardware: Linux writes
// `CMD SRV1 OPEN`, and changing that string means changing the Python/Bridge
// side too. What answers to it is direction + timed duration, not an angle —
// a DC motor has no absolute position, so "open"/"close" here means "drive
// that direction for MOTOR_DRIVE_MS, then coast".
//
// Three control lines, not two: this is a bare L298 with its own enable
// broken out, so EA is ours to drive rather than jumpered high on a module.
// That buys a real stop (enable low cuts the bridge without touching
// direction) on top of direction itself.
//
// Safe means safe with nobody watching: parking OPEN costs nothing (daylight
// still gets in), so that's the failsafe direction.
constexpr int PIN_MOTOR_IN1 = 13;   // "13"  L298 IA1 — direction
constexpr int PIN_MOTOR_IN2 = 12;   // "12"  L298 IA2 — direction
constexpr int PIN_MOTOR_ENA = 11;   // "11~" L298 EA  — bridge enable AND speed
// EA lives on a ~ pin deliberately: pin 4 (where it was first wired) has no
// hardware PWM and collided with the outdoor DHT22, so enable could only ever
// have been on/off there. On 11~ the same wire is a real speed knob.
constexpr int MOTOR_SPEED_FULL = 255;
// UNMEASURED — tune on the bench once the mechanism is built. This is a
// placeholder duration, not a calibrated travel time.
constexpr int FW_BUILD = 44;   // reported on the wire so the dashboard can
                               // prove which firmware is actually running
constexpr unsigned long MOTOR_DRIVE_MS = 1500;
// Hard ceiling on any commanded run. A bench control that can ask for an
// unbounded drive is a bench control that can stall the motor against a
// stop while nobody is watching; this is the one guard that prevents it.
constexpr int MOTOR_DRIVE_MAX_MS = 15000;
unsigned long blindsMotorRunMs = MOTOR_DRIVE_MS;

struct MotorState {
  int dir;                   // +1 = open direction, -1 = close, 0 = idle/coasting
  int speed;                 // 0..255, what ENA was last asked for
  unsigned long startedAt;
};
MotorState blindsMotor = {0, 0, 0};

// Direction on IN1/IN2, drive on ENA. Kept separate on purpose: a stop that
// drops ENA leaves the direction pins alone, so the next move does not have
// to re-establish anything.
void motorDrive(int dir, int speed) {
  if (speed < 0) speed = 0;
  if (speed > 255) speed = 255;
  // Direction flip while still driving: kill the bridge and give the
  // armature a moment to stop conducting before reversing. Plugging a
  // spinning motor spikes current past stall, and a bare L298 has no
  // flyback diodes to absorb it. 25 ms blocks less than one DHT read.
  if (blindsMotor.dir != 0 && dir != 0 && dir != blindsMotor.dir) {
    analogWrite(PIN_MOTOR_ENA, 0);
    delay(25);
  }
  blindsMotor.dir = dir;
  blindsMotor.speed = speed;
  blindsMotor.startedAt = millis();
  pinMode(PIN_MOTOR_IN1, OUTPUT);
  pinMode(PIN_MOTOR_IN2, OUTPUT);
  pinMode(PIN_MOTOR_ENA, OUTPUT);
  digitalWrite(PIN_MOTOR_IN1, dir > 0 ? HIGH : LOW);
  digitalWrite(PIN_MOTOR_IN2, dir < 0 ? HIGH : LOW);
  analogWrite(PIN_MOTOR_ENA, speed);
}

// Coast: enable low, bridge released, motor free-wheels to a halt. This is
// the resting state — a driver left enabled holds current for no benefit.
void motorCoast() {
  analogWrite(PIN_MOTOR_ENA, 0);
  digitalWrite(PIN_MOTOR_IN1, LOW);
  digitalWrite(PIN_MOTOR_IN2, LOW);
  blindsMotor.dir = 0;
  blindsMotor.speed = 0;
}

// Brake: both direction pins high with the bridge enabled shorts the motor
// across itself and stops it far harder than coasting. Offered because a
// blind that coasts past its endpoint is a blind that unwinds itself.
void motorBrake() {
  pinMode(PIN_MOTOR_IN1, OUTPUT);
  pinMode(PIN_MOTOR_IN2, OUTPUT);
  pinMode(PIN_MOTOR_ENA, OUTPUT);
  digitalWrite(PIN_MOTOR_IN1, HIGH);
  digitalWrite(PIN_MOTOR_IN2, HIGH);
  analogWrite(PIN_MOTOR_ENA, MOTOR_SPEED_FULL);
  blindsMotor.dir = 0;
  blindsMotor.speed = 0;
}

// Called every loop(). Ends a timed drive by coasting.
void motorPump() {
  if (!blindsMotor.dir) return;
  if (millis() - blindsMotor.startedAt >= blindsMotorRunMs) motorCoast();
}


// ── failsafe: the promise Linux cannot keep about itself ────────────────
// Anything arriving from upstairs is proof the Linux side is still running:
// every Bridge call — `breezeiq/read`, `breezeiq/cmd`, `breezeiq/display` —
// and every complete command line on the serial. Miss them all for
// FAILSAFE_SILENCE_MS and this MCU stops waiting for instructions and parks
// the room itself. That is the whole argument for a dual-brain board: a
// planner that has wedged cannot notice it has wedged.
//
// Only the Bridge path is periodic. The deployed reader polls `breezeiq/read`
// once a second, so the deadline is refreshed ~10x per window. The legacy
// monitor-TCP transport is outbound-only and gives no such heartbeat — on that
// path the failsafe engages between commands, which is correct behaviour for a
// development transport and the reason App Lab's RPC is the deployed one.
//
// Non-blocking by construction. The deadline is a millis() comparison checked
// from serviceIo(), the park drives through the same motor driver as a
// commanded move, and nothing here delays the 1 Hz sample.
constexpr unsigned long FAILSAFE_SILENCE_MS = 10000;

unsigned long lastHostCallAt = 0;
unsigned long failsafeEpisodes = 0;

// ── radar tuning ──────────────────────────────────────────────────────────
// `Serial` on this board is not the physical D0/D1 pins at all -- this
// variant declares an arduino_router_serial node, which reassigns `Serial` to
// BridgeMonitor (a virtual stream over Bridge RPC: mon/read, mon/write) and
// pushes the real hardware UART to `Serial1`. That means the 1 Hz debug line
// and commandPump()'s line parser were never on this wire in the first
// place -- there is no debug stream to pause, no command parser to race, and
// D0/D1 have been sitting genuinely idle the whole time. usart3 was the only
// other candidate and it shares PB10/PB11 with I2C, the bus the BH1750 needs,
// so Serial1 is not a workaround, it is the one free UART this board has.
constexpr unsigned long RADAR_TUNE_BAUD = 256000;   // the LD2410 family's fixed serial rate

String hexEncode(const uint8_t *buf, size_t n) {
  static const char *DIGITS = "0123456789abcdef";
  String out;
  out.reserve(n * 2);
  for (size_t i = 0; i < n; i++) {
    out += DIGITS[buf[i] >> 4];
    out += DIGITS[buf[i] & 0x0F];
  }
  return out;
}

// -1 on any non-hex character, which the caller treats as a malformed
// request rather than silently truncating or padding one that arrived
// slightly wrong.
int hexDecode(const String &hex, uint8_t *buf, size_t maxLen) {
  if (hex.length() % 2 != 0) return -1;
  size_t n = hex.length() / 2;
  if (n > maxLen) return -1;
  for (size_t i = 0; i < n; i++) {
    int hi = -1, lo = -1;
    for (int v = 0; v < 16; v++) {
      char c = "0123456789abcdef"[v];
      if (hex[i * 2] == c || hex[i * 2] == toupper(c)) hi = v;
      if (hex[i * 2 + 1] == c || hex[i * 2 + 1] == toupper(c)) lo = v;
    }
    if (hi < 0 || lo < 0) return -1;
    buf[i] = (uint8_t)((hi << 4) | lo);
  }
  return (int)n;
}

// A raw hex-in-hex-out relay over Serial1 -- this sketch does not parse
// LD2410 frames at all, which is deliberate: the protocol logic lives once,
// in the tool that already speaks it, rather than being reimplemented a
// second time on the MCU, where it is far slower to iterate on.
String handleRadarTune(String request) {
  // No blanket trim(): "CMD " with nothing after the space is a deliberate
  // empty-payload poll (see _poll() on the Python side), and trimming the
  // whole string first eats that trailing space before the prefix check ever
  // runs -- which was this function's own first bug, caught by that exact
  // poll returning "unknown request" against a sensor that was actually fine.
  // One-shot bench diagnostic, checked before hex validation since it takes
  // no payload: confirms write() itself believes it succeeded, which
  // separates "nothing is being sent" from "sent fine, never comes back" --
  // the two look identical from the read side alone. Not part of the normal
  // protocol relay.
  if (request == "DIAGWRITE") {
    uint8_t probe[8] = {0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55};
    size_t wrote = Serial1.write(probe, sizeof(probe));
    Serial1.flush();
    return "DIAG wrote=" + String((int)wrote)
         + " availForWrite=" + String(Serial1.availableForWrite());
  }
  if (!request.startsWith("CMD ")) return "ERR unknown request";
  String hex = request.substring(4);
  hex.trim();          // safe here: only the hex payload, never the prefix
  uint8_t out[64];
  int n = hexDecode(hex, out, sizeof(out));
  if (n < 0) return "ERR bad-hex";
  // Bounded, not "while available": this RPC runs in the same cooperative
  // loop() context as failsafeTick(), so an unbounded drain against a noisy
  // or floating RX line would starve the one thing that is supposed to park
  // the blinds when Linux goes quiet -- the exact failure the failsafe
  // exists to prevent, caused by the code meant to help diagnose a sensor.
  // A real stale backlog is at most a few frames; a continuous flood past
  // that is noise, and this stops reading it rather than chasing it.
  for (int i = 0; i < 512 && Serial1.available(); i++) Serial1.read();
  Serial1.write(out, n);
  Serial1.flush();
  uint8_t in[256];
  size_t got = 0;
  // 250 ms: generous next to the sensor's own report cadence, short next to a
  // human waiting on a CLI. A slow reply still returns whatever arrived
  // rather than blocking the whole tuning session on one lost frame.
  unsigned long deadline = millis() + 250;
  while (millis() < deadline && got < sizeof(in)) {
    if (Serial1.available()) in[got++] = (uint8_t)Serial1.read();
  }
  return hexEncode(in, got);
}

// Called from every inbound path. provide_safe runs the Bridge handlers in
// loop context and commandPump() is already there, so this shares one thread
// with failsafeTick() and needs no lock.
void linuxSeen() {
  lastHostCallAt = millis();
}

// Fires once per silence episode, re-arms the moment a call arrives.
void failsafeTick() {
  bool silent = millis() - lastHostCallAt >= FAILSAFE_SILENCE_MS;
  if (silent == failsafeActive) return;
  failsafeActive = silent;
  if (silent) {
    failsafeEpisodes++;
    motorDrive(+1, MOTOR_SPEED_FULL);   // park open — daylight costs nothing
  }
  drawMatrix();
  // Prefix EVT, not HW/ACK/ERR: the sensor readers key on "HW " and the
  // command link only offers "ACK "/"ERR " lines, so both ignore this by
  // shape. It exists for the serial monitor and the bench.
  Serial.print("EVT FAILSAFE ");
  Serial.print(silent ? "ENGAGED" : "CLEARED");
  Serial.print(" episode="); Serial.print(failsafeEpisodes);
  Serial.print(" silence_ms="); Serial.println(millis() - lastHostCallAt);
}

// ── command channel ─────────────────────────────────────────────────────
// Linux writes `CMD SRV1 OPEN` down the same serial the telemetry rides up.
// The two shapes cannot be confused: telemetry lines start with HW, replies
// with ACK or ERR, and neither parser accepts the other's prefix.
//
// ACK means accepted and driving. It cannot mean "the mechanism actually
// moved" — no feedback wire exists on this actuator, so no honest firmware
// can claim that.
//
// Named in replies to malformed lines, where no target was successfully read.
constexpr char MOTOR_TARGET[] = "SRV1";
constexpr size_t CMD_MAX = 48;

char cmdBuf[CMD_MAX];
size_t cmdLen = 0;
bool cmdOverflow = false;

String replyLine(const char *verb, const char *target, const char *what) {
  return String(verb) + " " + target + " " + what;
}

// Splits on blanks in place. Hand-rolled because strtok keeps global state
// between calls and the serial and RPC paths both parse through here, while
// strtok_r is declared but not linkable in this core's libc (measured: the
// same class of gap as noInterrupts() at the top of this file).
char *nextToken(char **cursor) {
  char *s = *cursor;
  while (*s == ' ' || *s == '\t') s++;
  if (!*s) { *cursor = s; return nullptr; }
  char *start = s;
  while (*s && *s != ' ' && *s != '\t') s++;
  if (*s) *s++ = '\0';
  *cursor = s;
  return start;
}

// atoi() answers 0 for "banana", which would quietly show EMPTY for a garbled
// payload. Digits only, and a hard ceiling so no payload can overflow the int.
bool parseSmallInt(const char *text, int &out) {
  if (text == nullptr || !*text) return false;
  int value = 0;
  for (const char *p = text; *p; p++) {
    if (*p < '0' || *p > '9') return false;
    value = value * 10 + (*p - '0');
    if (value > 99) return false;
  }
  out = value;
  return true;
}

// Same digits-only discipline, caller-supplied ceiling. parseSmallInt's own
// 99 cap is right for the display's fields but silently rejects a motor
// speed of 100 and any run time worth naming, so those parse through here.
bool parseBounded(const char *text, int max, int &out) {
  if (text == nullptr || !*text) return false;
  long value = 0;
  for (const char *p = text; *p; p++) {
    if (*p < '0' || *p > '9') return false;
    value = value * 10 + (*p - '0');
    if (value > max) return false;
  }
  out = (int)value;
  return true;
}

// ── the matrix as a status surface Linux can write to ───────────────────
// The occupancy mode and people count are Linux's to know: a camera and a
// tracker live up there, not down here. The MCU only renders what it is told
// and refuses anything it cannot render honestly.
constexpr char DISPLAY_TARGET[] = "DISP";

String runDisplay(char *payload) {
  char *cursor = payload;
  int mode = 0, people = 0;
  if (!parseSmallInt(nextToken(&cursor), mode)
      || !parseSmallInt(nextToken(&cursor), people))
    return replyLine("ERR", DISPLAY_TARGET, "bad-payload");
  if (mode >= OCCUPANCY_MODES)
    return replyLine("ERR", DISPLAY_TARGET, "unknown-mode");
  if (people > PEOPLE_MAX)
    return replyLine("ERR", DISPLAY_TARGET, "too-many-people");
  occupancyMode = mode;
  peopleCount = people;
  drawMatrix();
  return String("ACK ") + DISPLAY_TARGET + " " + mode + " " + people;
}

String runCommand(char *line) {
  char *cursor = line;
  char *verb = nextToken(&cursor);
  if (verb == nullptr || strcmp(verb, "CMD") != 0)
    return replyLine("ERR", MOTOR_TARGET, "not-a-command");
  char *target = nextToken(&cursor);
  if (target == nullptr) return replyLine("ERR", MOTOR_TARGET, "missing-target");
  // The display shares the command grammar so one line reaches the panel over
  // the serial and over Bridge RPC alike. `breezeiq/display` is the same
  // parser behind a dedicated door, not a second implementation.
  if (strcmp(target, DISPLAY_TARGET) == 0) return runDisplay(cursor);
  char *what = nextToken(&cursor);
  if (what == nullptr) return replyLine("ERR", target, "missing-position");
  // SRV1 names the curtain/blinds actuator on the wire — see the comment
  // above motorDrive(). It's the only actuator target.
  //
  // `CMD SRV1 OPEN` takes an optional speed and duration after it, plus
  // STOP and BRAKE: a bare L298 with its own enable line can do things a
  // jumpered module cannot.
  if (strcmp(target, "SRV1") == 0) {
    if (strcmp(what, "STOP") == 0)  { motorCoast(); return replyLine("ACK", target, "STOP"); }
    if (strcmp(what, "BRAKE") == 0) { motorBrake(); return replyLine("ACK", target, "BRAKE"); }
    int dir = 0;
    if (strcmp(what, "OPEN") == 0) dir = +1;
    else if (strcmp(what, "CLOSE") == 0) dir = -1;
    else return replyLine("ERR", target, "unknown-position");
    // Both extras are optional and default to what the caller got before
    // they existed, so every existing caller keeps working untouched. A
    // garbled value is refused rather than guessed at.
    int speed = MOTOR_SPEED_FULL;
    char *speedText = nextToken(&cursor);
    if (speedText != nullptr) {
      int pct = 0;
      if (!parseBounded(speedText, 100, pct))
        return replyLine("ERR", target, "bad-speed");
      speed = (pct * MOTOR_SPEED_FULL) / 100;
    }
    // Run time in milliseconds, capped at MOTOR_DRIVE_MAX_MS. The cap is the
    // whole safety story for a bench control: a mistyped duration cannot
    // leave the motor driving into a stop indefinitely.
    char *msText = nextToken(&cursor);
    if (msText != nullptr) {
      int ms = 0;
      if (!parseBounded(msText, MOTOR_DRIVE_MAX_MS, ms) || ms == 0)
        return replyLine("ERR", target, "bad-duration");
      blindsMotorRunMs = (unsigned long)ms;
    } else {
      blindsMotorRunMs = MOTOR_DRIVE_MS;
    }
    motorDrive(dir, speed);
    return replyLine("ACK", target, dir > 0 ? "OPEN" : "CLOSE");
  }
  return replyLine("ERR", target, "unknown-target");
}

// Reads whatever has arrived and returns; a partial line simply waits for the
// next call. Never blocks on the serial, so a half-typed command in the App Lab
// Monitor cannot stall the sensor loop.
void commandPump() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (cmdLen < CMD_MAX - 1) cmdBuf[cmdLen++] = c;
      else cmdOverflow = true;
      continue;
    }
    cmdBuf[cmdLen] = '\0';
    // A complete line from upstairs is proof of life too, whatever it says —
    // even a rejected one took a running process to type.
    linuxSeen();
    if (cmdOverflow)
      Serial.println(replyLine("ERR", MOTOR_TARGET, "line-too-long"));
    else if (cmdLen)
      Serial.println(runCommand(cmdBuf));
    cmdLen = 0;
    cmdOverflow = false;
  }
}

// App Lab runs Python in a container where the monitor port is unreachable, so
// the same command arrives as RPC instead. One parser serves both.
String handleBridgeCommand(String request) {
  linuxSeen();
  char line[CMD_MAX];
  request.trim();
  strncpy(line, request.c_str(), sizeof line - 1);
  line[sizeof line - 1] = '\0';
  return runCommand(line);
}

// A dedicated door for callers holding App Lab's Bridge directly, which take
// `"<mode> <people>"` with no CMD wrapper. Same parser as the command path.
String handleBridgeDisplay(String request) {
  linuxSeen();
  char line[CMD_MAX];
  request.trim();
  strncpy(line, request.c_str(), sizeof line - 1);
  line[sizeof line - 1] = '\0';
  return runDisplay(line);
}

// App Lab runs Python in a container. The board-native Bridge socket is mounted
// into that container, while the serial-monitor TCP port is intentionally host
// loopback-only. Expose the latest compact sensor frame as a short, safe RPC.
// provide_safe executes this in loop context, so Arduino String access cannot
// race the sensor-update code below.
String readSensorSnapshot() {
  linuxSeen();
  return latestSnapshot;
}

void setup() {
  Bridge.begin();
  Serial.begin(115200);
  while (!Serial && millis() < 4000) {}

  analogReadResolution(12);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RADAR, INPUT);

  // Read the SCL/SDA pads as plain digital inputs before any I2C peripheral
  // claims them. A powered BH1750 holds both HIGH through its own on-board
  // pull-ups; both LOW settles the question before a single address is
  // probed — nothing on those pads is powered, and no scan changes that.
  // One-shot at boot: once Wire2 owns these pins the read no longer means
  // what it did here.
  pinMode(A4, INPUT);
  pinMode(A5, INPUT);
  delay(2);
  sdaPullup = digitalRead(A4) == HIGH;
  sclPullup = digitalRead(A5) == HIGH;
  // Same two pins through a different peripheral, because a digital read on
  // the wrong pin and an unpowered sensor produce the identical answer. The
  // ADC does not share the GPIO mapping, so agreement between them is real
  // evidence and disagreement says the mapping is what is wrong.
  sdaLevel = analogRead(A4);
  sclLevel = analogRead(A5);

  for (int b = 0; b < I2C_BUS_COUNT; b++) {
    I2C_BUSES[b]->begin();
    // 100 kHz, not the 400 kHz the variant configures. Fast mode wants short
    // traces and firm pull-ups; this is a breadboard hop with only the
    // module's own resistors on the line, and a bus that is marginal at 400
    // simply NACKs — which is indistinguishable from an absent part. Standard
    // mode costs nothing here: two bytes once a second.
    I2C_BUSES[b]->setClock(100000);
  }
  delay(10);            // BH1750 needs a moment after power before it answers
  i2cDevices = i2cScan();
  bh1750Begin();

  matrix.begin();
  matrix.setGrayscaleBits(8);        // frame bytes are 0..255, not 0..7

  dhtIndoor.begin();
  dhtOutdoor.begin();

  Bridge.provide_safe("breezeiq/read", readSensorSnapshot);
  Bridge.provide_safe("breezeiq/cmd", handleBridgeCommand);
  Bridge.provide_safe("breezeiq/display", handleBridgeDisplay);
  Bridge.provide_safe("breezeiq/radar_tune", handleRadarTune);
  Serial1.begin(RADAR_TUNE_BAUD);   // the physical D0/D1 UART; nothing else uses it

  // Arm the deadline at boot, not at the first call. A board that comes up
  // with no Linux beside it has exactly the problem the failsafe exists for,
  // and 10 s later it parks the room instead of holding whatever position the
  // horns happened to be left in.
  lastHostCallAt = millis();

  Serial.println();
  Serial.println("BreezeIQ hwcheck BUILD=" + String(FW_BUILD) + " motor-guard-and-wire-fields - one line per second");
  Serial.println("channels: dht_in(7) dht_out(4) ldr(A0) pir(8) mq(A1) radar(2, dead)");
  Serial.println("commands: CMD SRV1 OPEN|CLOSE  -> ACK/ERR <target> ...");
  Serial.println("display:  CMD DISP <mode 0-3> <people 0-9> -> ACK DISP m p");
  Serial.println("failsafe: 10s without a Bridge call -> SRV1 OPEN (curtain/blinds motor)");
}

// Sensor sampling stays on its own clock; command parsing and the motor's
// timed coast are serviced in the gap instead of a delay(). Both are also
// kept strictly outside the telemetry print below, so a reply can never land
// inside an HW line and break the host parser's framing.
void serviceIo() {
  commandPump();
  failsafeTick();               // ~1 kHz deadline check, no delay of its own
  motorPump();
  delay(1);                     // one tick to the bridge thread, not a wait
}

void loop() {
  // The library rate-limits internally (a DHT22 samples at 0.5 Hz) and returns
  // its cached frame in between, so calling once a second is fine.
  float tInRaw  = dhtIndoor.readTemperature(),  hIn  = dhtIndoor.readHumidity();
  float tOutRaw = dhtOutdoor.readTemperature(), hOut = dhtOutdoor.readHumidity();

  Status sIn  = isnan(tInRaw)  ? ST_NO_RESPONSE : ST_OK;
  Status sOut = isnan(tOutRaw) ? ST_NO_RESPONSE : ST_OK;
  float tIn  = isnan(tInRaw)  ? tInRaw  : tInRaw  + TEMP_OFFSET_IN_C;
  float tOut = isnan(tOutRaw) ? tOutRaw : tOutRaw + TEMP_OFFSET_OUT_C;

  // A4 and A5 are the I²C bus. Sampling them as ADC channels was harmless
  // while nothing was attached — they were the reference pins the host uses to
  // recognise a floating input — but a driven bus reads like a live sensor and
  // would turn that reference into a lie. A0 replaces them once the LDR goes.
  const int adcPins[4] = {A0, A1, A2, A3};
  int adcValue[4], adcSpan[4];
  for (int i = 0; i < 4; i++)
    readAnalogStable(adcPins[i], adcValue[i], adcSpan[i]);

  int   ldr    = adcValue[0];
  int   mq     = adcValue[1];
  bool  motion = readPir(PIN_PIR);
  bool  radar  = readRadar(PIN_RADAR);
  if (!bh1750Addr) { i2cDevices = i2cScan(); bh1750Begin(); }
  float lux    = readBh1750();

  // ── tuning accumulators ───────────────────────────────────────────────
  // These are what t1..t5 would print. Computed here so you can tune while
  // watching the dashboard, instead of reflashing between tune and observe.
  dhtTry++;
  if (!isnan(tIn)) {
    dhtOk++;
    if (tIn < dhtMinC) dhtMinC = tIn;
    if (tIn > dhtMaxC) dhtMaxC = tIn;
  }
  // Only track the LDR range once the pin is genuinely driven. A floating or
  // half-built divider would otherwise pin dark at 0 and sun at 4095 and the
  // captured constants would be pure noise.
  if (ldr > ADC_RAIL_MARGIN && ldr < ADC_MAX - ADC_RAIL_MARGIN
      && adcSpan[0] < 300) {
    if (ldr < ldrMin) ldrMin = ldr;
    if (ldr > ldrMax) ldrMax = ldr;
  }
  if (motion && !pirPrev) { pirHighSince = millis(); pirEvents++; }
  if (!motion && pirPrev && pirHighSince) pirLastPulse = millis() - pirHighSince;
  pirPrev = motion;

  // MQ baseline = cleanest air seen = highest sensor resistance.
  if (mq > ADC_RAIL_MARGIN && mq < ADC_MAX - ADC_RAIL_MARGIN) {
    float vOut = ((mq / 4095.0f) * 3.3f) / 0.6f;
    vOut = vOut > 4.99f ? 4.99f : (vOut < 0.01f ? 0.01f : vOut);
    float rs = (5.0f - vOut) / vOut * 10.0f;
    if (isnan(mqR0k) || rs > mqR0k) mqR0k = rs;
  }

  // A lit column means "this channel produced a usable reading just now", and
  // nothing stronger. The DHTs prove themselves by answering; the PIR shows
  // its live level; the two ADC channels light when they sit off both rails,
  // which is the strongest honest statement this board can make about an
  // analogue pin — see the note above readAnalogStable() for why "connected"
  // is a host-side verdict. An unlit LDR column is real information, not a
  // placeholder: it means that pin is pinned to a rail right now.
  channelOk[0] = sIn == ST_OK;
  channelOk[1] = sOut == ST_OK;
  channelOk[2] = ldr > ADC_RAIL_MARGIN && ldr < ADC_MAX - ADC_RAIL_MARGIN;
  channelOk[3] = motion;
  channelOk[4] = mq > ADC_RAIL_MARGIN && mq < ADC_MAX - ADC_RAIL_MARGIN;
  drawMatrix();

  // Rails cannot be measured from the MCU, only inferred from a sensor that
  // answers. A DHT that replies proves its own 3.3 V. The analogue heuristics
  // cannot prove anything, so they are not allowed to claim a rail.
  const char *rail33 = (sIn == ST_OK || sOut == ST_OK) ? "PROVEN" : "UNPROVEN";
  const char *rail5  = "UNPROVEN";   // nothing on rail B can prove itself to the MCU

  latestSnapshot = "HW in_c=" + String(isnan(tIn) ? 0.0f : tIn, 1)
    + " in_rh=" + String(isnan(hIn) ? 0.0f : hIn, 1)
    + " out_c=" + String(isnan(tOut) ? 0.0f : tOut, 1)
    + " out_rh=" + String(isnan(hOut) ? 0.0f : hOut, 1)
    // The uncalibrated readings, beside the corrected ones rather than
    // instead of them. TEMP_OFFSET_*_C is applied on this board, so without
    // these the offset is unrecoverable downstream and telemetry's own promise
    // — raw values stay beside calibrated ones so a future recalibration does
    // not have to rewrite history — is quietly broken for the two channels the
    // whole comfort calculation rests on.
    + " in_raw_c=" + String(isnan(tInRaw) ? 0.0f : tInRaw, 1)
    + " out_raw_c=" + String(isnan(tOutRaw) ? 0.0f : tOutRaw, 1)
    + " ldr=" + String(ldr)
    + " mq=" + String(mq)
    + " pir=" + String(motion ? 1 : 0)
    + " s_in=" + String(statusName(sIn))
    + " s_out=" + String(statusName(sOut))
    + " ldr_min=" + String(ldrMin == ADC_MAX ? -1 : ldrMin)
    + " ldr_max=" + String(ldrMax)
    // Both readers parse an HW line as loose key=value pairs and ignore keys
    // they do not know, so these ride the existing frame rather than a second
    // channel. The flag is the live state; the counter is what survives the
    // gap, because a Linux side that was silent through the episode is by
    // definition not reading while the flag is up.
    + " failsafe=" + String(failsafeActive ? 1 : 0)
    + " failsafe_n=" + String(failsafeEpisodes)
    // lux=-1 is "the part did not ACK", not darkness. The reader keeps that
    // distinction; it is the difference between a missing sensor and a night.
    + " lux=" + String(lux, 1)
    + " lux_addr=" + String(bh1750Addr)
    + " i2c_n=" + String(i2cDevices)
    + " i2c_b0=" + String(busDevices[0])
    + " i2c_b1=" + String(busDevices[1])
    + " i2c_b2=" + String(busDevices[2])
    + " lux_bus=" + String(bh1750Bus)
    + " sda_pu=" + String(sdaPullup ? 1 : 0)
    + " scl_pu=" + String(sclPullup ? 1 : 0)
    + " sda_lvl=" + String(sdaLevel)
    + " scl_lvl=" + String(sclLevel)
    + " radar=" + String(radar ? 1 : 0)
    // The two figures that tell a working radar from a stuck pin. See readRadar().
    + " radar_edges=" + String(radarEdges)
    + " radar_held_ms=" + String(millis() - radarHeldSince)
    // Motor state, so the Workbench shows what the actuator was last told
    // rather than what somebody hoped it did. No feedback wire exists.
    + " motor_dir=" + String(blindsMotor.dir)
    + " motor_speed=" + String(blindsMotor.speed)
    // How much of the commanded window is left — ground truth for the bench
    // panel's countdown, which otherwise runs on the browser's own clock.
    + " motor_left_ms=" + String(blindsMotor.dir == 0 ? 0
        : (millis() - blindsMotor.startedAt >= blindsMotorRunMs ? 0
           : blindsMotorRunMs - (millis() - blindsMotor.startedAt)))
    + " fw_build=" + String(FW_BUILD);

  Serial.print("HW");
  Serial.print(" in_c=");   Serial.print(isnan(tIn)  ? 0.0f : tIn, 1);
  Serial.print(" in_raw_c="); Serial.print(isnan(tInRaw) ? 0.0f : tInRaw, 1);
  Serial.print(" in_rh=");  Serial.print(isnan(hIn)  ? 0.0f : hIn, 1);
  Serial.print(" out_c=");  Serial.print(isnan(tOut) ? 0.0f : tOut, 1);
  Serial.print(" out_raw_c="); Serial.print(isnan(tOutRaw) ? 0.0f : tOutRaw, 1);
  Serial.print(" out_rh="); Serial.print(isnan(hOut) ? 0.0f : hOut, 1);
  Serial.print(" temp_ref_c="); Serial.print(TEMP_REFERENCE_C, 1);
  Serial.print(" in_offset_c="); Serial.print(TEMP_OFFSET_IN_C, 1);
  Serial.print(" out_offset_c="); Serial.print(TEMP_OFFSET_OUT_C, 1);
  Serial.print(" ldr=");    Serial.print(ldr);
  Serial.print(" mq=");     Serial.print(mq);
  Serial.print(" pir=");    Serial.print(motion ? 1 : 0);
  Serial.print(" s_in=");   Serial.print(statusName(sIn));
  Serial.print(" s_out=");  Serial.print(statusName(sOut));
  Serial.print(" rail33="); Serial.print(rail33);
  Serial.print(" rail5=");  Serial.print(rail5);
  Serial.print(" ldr_min=");   Serial.print(ldrMin == ADC_MAX ? -1 : ldrMin);
  Serial.print(" ldr_max=");   Serial.print(ldrMax);
  Serial.print(" dht_ok=");    Serial.print(dhtTry ? (100 * dhtOk) / dhtTry : 0);
  Serial.print(" dht_noise="); Serial.print(dhtOk ? dhtMaxC - dhtMinC : 0.0f, 1);
  Serial.print(" pir_events="); Serial.print(pirEvents);
  Serial.print(" pir_pulse=");  Serial.print(pirLastPulse);
  Serial.print(" mq_r0=");      Serial.print(isnan(mqR0k) ? -1.0f : mqR0k, 1);
  Serial.print(" failsafe=");   Serial.print(failsafeActive ? 1 : 0);
  Serial.print(" failsafe_n="); Serial.print(failsafeEpisodes);
  Serial.print(" lux=");        Serial.print(lux, 1);
  Serial.print(" lux_addr=");   Serial.print(bh1750Addr);
  Serial.print(" i2c_n=");      Serial.print(i2cDevices);
  Serial.print(" sda_pu=");     Serial.print(sdaPullup ? 1 : 0);
  Serial.print(" scl_pu=");     Serial.print(sclPullup ? 1 : 0);
  Serial.print(" radar=");      Serial.print(radar ? 1 : 0);
  // Four now, not six: A4/A5 became the I²C bus. Reading past the array here
  // was the bug this loop would have shipped with, printing stack bytes as
  // ADC counts on the one line a human uses to trust the board.
  for (int i = 0; i < 4; i++) {
    Serial.print(" a"); Serial.print(i); Serial.print("=");
    Serial.print(adcValue[i]);
    Serial.print(" a"); Serial.print(i); Serial.print("_span=");
    Serial.print(adcSpan[i]);
  }
  Serial.println();

  // Hold 1 Hz without blocking: the motor's run timer needs pumping and a
  // command must not wait a second for its ACK.
  nextSampleAt = millis() + SAMPLE_PERIOD_MS;
  while ((long)(millis() - nextSampleAt) < 0) serviceIo();
}
