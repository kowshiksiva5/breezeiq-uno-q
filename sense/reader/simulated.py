"""Simulated room — lets the whole stack run with no hardware attached.

Not a stub: it reuses the same lumped-RC model the seasonal sweep is validated
against, so a dashboard demo without the rig still behaves like a real room.
"""
from __future__ import annotations

import math
import time

from .base import SensorFrame, SensorSource, register_source


@register_source("mock")
class MockSensorSource(SensorSource):
    def __init__(self, start_hour: float = 10.0, **kwargs):
        super().__init__(**kwargs)
        self._t0 = time.time()
        self._start_hour = start_hour

    def _read_raw(self) -> SensorFrame:
        # 1 simulated minute per wall-clock second, matching the live harness.
        hour = (self._start_hour + (time.time() - self._t0) / 60.0) % 24
        indoor = 27.0 + 2.0 * math.sin((hour - 6) / 24 * 2 * math.pi)
        outdoor = 30.0 + 5.0 * math.sin((hour - 8) / 24 * 2 * math.pi)
        solar = max(0.0, math.sin((hour - 6) / 12 * math.pi))
        return SensorFrame(
            indoor_c=round(indoor, 2), indoor_rh=55.0,
            outdoor_c=round(outdoor, 2), outdoor_rh=50.0,
            light_raw=int(solar * 4000), solar_index=round(solar, 3),
            air_raw=450, occupied=(int(hour) % 4 != 0),
        )
