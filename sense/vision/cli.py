"""Test and tune the people counter.

    ./tools/run.sh vision selftest         policy only, no camera, no model
    ./tools/run.sh vision once             one count from the Mac camera
    ./tools/run.sh vision snap             one count + annotated JPEG to look at
    ./tools/run.sh vision watch 120        live count for 120 s, 1 Hz
    ./tools/run.sh vision run              the real thing: PIR-triggered policy
    ./tools/run.sh vision tune             sweep confidence, pick a threshold

Start with `selftest` — it proves the sampling policy is right before a camera
is involved at all. Then `snap`, and look at the JPEG: if the boxes are wrong,
no amount of policy tuning will save the count.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sense"))

from vision.counter import build_counter, available_counters          # noqa: E402
from vision import policy                                            # noqa: E402
from vision.evaluate import count_metrics                            # noqa: E402

DEBUG_DIR = Path(__file__).resolve().parent / "debug"


def cmd_selftest(_args) -> int:
    """Drive the policy through a scripted day. No camera, no model."""
    print("sampling policy — scripted timeline, no hardware\n")
    st = policy.VisionState()
    # (minute, pir_active, what is actually happening)
    timeline = [
        (0.0,  False, "cold start"),
        (0.1,  False, "just looked"),
        (2.0,  False, "quiet"),
        (5.1,  False, "baseline due"),
        (6.0,  True,  "SOMEONE WALKS IN"),
        (6.5,  True,  "still moving"),
        (7.0,  False, "settled, reading"),
        (12.0, False, "quiet 6 min"),
        (17.1, False, "PIR quiet 10 min while occupied"),
        (17.5, False, "just looked"),
        (23.0, False, "baseline due"),
        (30.0, True,  "WALKS IN AGAIN"),
    ]
    fired = 0
    for minute, pir, note in timeline:
        d = policy.should_capture(minute, pir, st)
        st = d.state
        if d.capture:
            fired += 1
            # pretend the camera saw someone whenever PIR is warm
            st = policy.record_count(st, 1 if pir or "occupied" in note else 0)
        mark = "CAPTURE" if d.capture else "   ·   "
        print(f"  t={minute:5.1f}m  pir={int(pir)}  {mark}  "
              f"{d.reason:<12} count={st.last_count}   {note}")
    print(f"\n{fired} captures in 30 simulated minutes.")
    print("A continuously occupied room must not retrigger arrival — check above.")
    return 0


def _open(args):
    c = build_counter(args.counter, camera=args.camera, confidence=args.confidence)
    return c.open()


def cmd_once(args) -> int:
    c = _open(args)
    try:
        f = c.count()
        print(f"count={f.count}  valid={f.valid}  conf={f.confidence:.2f}  "
              f"{f.inference_ms:.0f} ms  {f.fault}")
        return 0 if f.valid else 1
    finally:
        c.close()


def cmd_snap(args) -> int:
    c = _open(args)
    try:
        f = c.count()
        DEBUG_DIR.mkdir(exist_ok=True)
        out = DEBUG_DIR / f"snap-{int(time.time())}.jpg"
        saved = c.save_debug_frame(out) if hasattr(c, "save_debug_frame") else None
        print(f"count={f.count}  conf={f.confidence:.2f}  {f.inference_ms:.0f} ms")
        print(f"wrote {saved}" if saved else "no frame to write")
        print("\nOpen it. Boxes on people = good. Boxes on a chair = raise "
              "--confidence. People missed = lower it.")
        return 0
    finally:
        c.close()


def cmd_watch(args) -> int:
    c = _open(args)
    seconds = args.seconds
    try:
        print(f"watching {seconds}s at 1 Hz — move around, sit still, leave frame\n")
        t0 = time.time()
        hist: list[int] = []
        while time.time() - t0 < seconds:
            f = c.count()
            hist.append(f.count if f.count is not None else -1)
            bar = "#" * (f.count or 0)
            print(f"  {time.strftime('%H:%M:%S')}  count={f.count}  "
                  f"{f.inference_ms:5.0f} ms  {bar}")
            time.sleep(1.0)
        good = [h for h in hist if h >= 0]
        if good:
            print(f"\n{len(good)}/{len(hist)} good reads · "
                  f"min {min(good)} max {max(good)} · "
                  f"stability {100 * good.count(max(set(good), key=good.count)) // len(good)}% "
                  "on the modal value")
            print("Jitter between two values while you sat still means the "
                  "confidence threshold is near a box's score — tune it.")
        return 0
    finally:
        c.close()


def cmd_tune(args) -> int:
    """Sweep the confidence threshold against the scene in front of the camera.
    Sit still with a known number of people visible, then pick the widest
    threshold band that reports that number."""
    import numpy as np
    c = build_counter(args.counter, camera=args.camera, confidence=0.05).open()
    try:
        print(f"hold still — expecting {args.expect} person(s) in frame\n")
        raw_scores: list[float] = []
        for _ in range(args.frames):
            c.count()
            raw_scores += [s for (*_b, s) in getattr(c, "last_boxes", [])]
            time.sleep(0.2)
        if not raw_scores:
            print("no detections at all at confidence 0.05 — the model cannot "
                  "see anyone. Check lighting and framing before tuning.")
            return 1
        print(f"{len(raw_scores)} boxes over {args.frames} frames")
        print(f"score range {min(raw_scores):.2f} .. {max(raw_scores):.2f}\n")
        print(f"{'threshold':>10}{'mean count':>12}")
        for th in np.arange(0.20, 0.85, 0.05):
            n = sum(1 for s in raw_scores if s >= th) / args.frames
            flag = "  <-- matches" if abs(n - args.expect) < 0.25 else ""
            print(f"{th:>10.2f}{n:>12.2f}{flag}")
        print("\nPick the middle of the widest matching band, not its edge.")
        return 0
    finally:
        c.close()


def cmd_collect(args) -> int:
    """Gather training frames for the Edge Impulse model.

    THE ONE PLACE FRAMES ARE EVER STORED — a human ran this on purpose, for
    training, and the frames stay local until that human uploads them to EI
    Studio. The runtime never stores an image; this command is the documented
    exception, which is exactly what the privacy section of the write-up says.

    Usage: sit in frame, move around, leave, come back with a second person.
    Vary the lighting between runs (morning / afternoon / lamp).
    """
    import cv2
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    c = _open(args)
    try:
        n = len(list(out.glob("*.jpg")))
        print(f"collecting {args.frames} frames every {args.every}s -> {out}/")
        print("vary: people count, positions, lighting. Ctrl-C to stop early.\n")
        for i in range(args.frames):
            f = c.count()                      # reuses the pipeline's capture
            img = getattr(c, "_last_bgr", None)
            if img is not None:
                # 320x320 is plenty for FOMO's 96x96 input and keeps uploads small
                small = cv2.resize(img, (320, 320))
                name = out / f"room-{n + i:04d}.jpg"
                cv2.imwrite(str(name), small)
                print(f"  {name.name}   (opencv counted {f.count} — label truth yourself)")
            time.sleep(args.every)
        print(f"\nnext: upload {out}/ to Edge Impulse Studio, label 'person',")
        print("train FOMO 96x96, export 'Linux (AARCH64)' -> sense/vision/models/person.eim")
        return 0
    finally:
        c.close()


def cmd_run(args) -> int:
    """The real loop: policy decides when, counter answers how many.

    PIR is not wired into this CLI — it lives on the MCU. `--pir-every` fakes an
    arrival on a fixed cadence so the wiring can be watched end to end before
    the two are joined in `brain/loop.py`.
    """
    c = _open(args)
    st = policy.VisionState()
    t0 = time.time()
    try:
        print(f"policy-driven for {args.seconds}s "
              f"(1 s tick = {args.minutes_per_tick} simulated min)\n")
        tick = 0
        while time.time() - t0 < args.seconds:
            tick += 1
            now_min = tick * args.minutes_per_tick
            pir = args.pir_every > 0 and tick % args.pir_every == 0
            d = policy.should_capture(now_min, pir, st)
            st = d.state
            if d.capture:
                f = c.count()
                st = policy.record_count(st, f.count)
                print(f"  t={now_min:6.1f}m  {d.reason:<10} count={f.count}  "
                      f"{f.inference_ms:.0f} ms")
            time.sleep(1.0)
        return 0
    finally:
        c.close()


def cmd_evaluate(args) -> int:
    """Run labeled still scenes and report count error plus confusion pairs.

    CSV format: ``image,expected``. Paths are relative to the CSV. Include
    empty, one-person, and multi-person scenes or ``acceptance_ready`` stays
    false. Runtime frames are still discarded; this explicit labeled set is a
    test artifact controlled by the operator.
    """
    manifest = Path(args.manifest).resolve()
    rows = list(csv.DictReader(manifest.open(newline="")))
    expected, predicted, details = [], [], []
    for row in rows:
        image = (manifest.parent / row["image"]).resolve()
        want = int(row["expected"])
        counter = build_counter(args.counter, camera=str(image),
                                confidence=args.confidence).open()
        try:
            frame = counter.count()
        finally:
            counter.close()
        expected.append(want)
        predicted.append(frame.count if frame.valid else None)
        details.append({"image": row["image"], "expected": want,
                        "predicted": frame.count if frame.valid else None,
                        "confidence": frame.confidence,
                        "fault": frame.fault})
    report = count_metrics(expected, predicted)
    report["cases"] = details
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_ready"] and report["invalid"] == 0 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vision")
    p.add_argument("--counter", default="opencv", choices=available_counters())
    p.add_argument("--camera", default=0, type=lambda v: int(v) if str(v).isdigit() else v,
                   help="0 = Mac built-in; or a device path")
    p.add_argument("--confidence", type=float, default=0.45)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest")
    sub.add_parser("once")
    sub.add_parser("snap")
    w = sub.add_parser("watch")
    w.add_argument("seconds", nargs="?", type=int, default=30)
    t = sub.add_parser("tune")
    t.add_argument("--expect", type=int, default=1)
    t.add_argument("--frames", type=int, default=20)
    col = sub.add_parser("collect")
    col.add_argument("--frames", type=int, default=60)
    col.add_argument("--every", type=float, default=3.0)
    col.add_argument("--outdir", default=str(Path(__file__).parent / "dataset"))
    r = sub.add_parser("run")
    r.add_argument("seconds", nargs="?", type=int, default=60)
    r.add_argument("--minutes-per-tick", type=float, default=1.0)
    r.add_argument("--pir-every", type=int, default=0, help="fake a PIR hit every N ticks")
    ev = sub.add_parser("evaluate")
    ev.add_argument("manifest", help="CSV with image,expected columns")

    a = p.parse_args(argv)
    return {"selftest": cmd_selftest, "once": cmd_once, "snap": cmd_snap,
            "watch": cmd_watch, "tune": cmd_tune, "run": cmd_run,
            "collect": cmd_collect, "evaluate": cmd_evaluate}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
