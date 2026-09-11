#!/usr/bin/env bash
# MobileNet-SSD weights for sense/vision. ~23 MB, fetched once, gitignored.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/../sense/vision/models" && pwd)"
BASE="https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master"
[ -f "$DIR/MobileNetSSD_deploy.prototxt" ] || \
  curl -sSL -o "$DIR/MobileNetSSD_deploy.prototxt" "$BASE/deploy.prototxt"
[ -f "$DIR/MobileNetSSD_deploy.caffemodel" ] || \
  curl -sSL -o "$DIR/MobileNetSSD_deploy.caffemodel" \
    "https://github.com/chuanqi305/MobileNet-SSD/raw/master/mobilenet_iter_73000.caffemodel"
ls -la "$DIR"

# YOLO11s for the `yolo` counter — the model the board actually runs.
#
# Chosen by measurement, not reputation: scored against 120 hand-labelled
# frames of the real room, YOLO11s got the count exactly right 71% of the
# time (MAE 0.30) where the App Lab Brick managed 10% (MAE 0.90). YOLO11m
# scored WORSE (61%), so do not "upgrade" it without re-running the bench.
#
# ~38 MB, gitignored. Export needs ultralytics on the build host only; the
# board runs the .onnx through onnxruntime and never needs torch.
if [ ! -f "$DIR/yolo11s.onnx" ]; then
  echo "exporting yolo11s.onnx (needs ultralytics on this host)"
  python3 -c "
from ultralytics import YOLO
import shutil
path = YOLO('yolo11s.pt').export(format='onnx', imgsz=640, simplify=True, opset=12)
shutil.move(str(path), '$DIR/yolo11s.onnx')
" || echo "  export failed — pip install ultralytics, then re-run" >&2
fi
ls -la "$DIR"
