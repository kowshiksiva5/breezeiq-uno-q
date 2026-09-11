"""Layer 1 — sensing.

One job: produce a validated SensorFrame, whatever the source. The rest of the
program must not know or care whether that came from a serial cable, the Bridge
RPC, or a simulation.

Add a source by subclassing SensorSource and registering it. Nothing else in the
codebase changes.
"""
from .base import SensorFrame, SensorSource, register_source, build_source, available_sources
from . import board, router, simulated          # noqa: F401  (self-registering)

__all__ = ["SensorFrame", "SensorSource", "register_source",
           "build_source", "available_sources"]
