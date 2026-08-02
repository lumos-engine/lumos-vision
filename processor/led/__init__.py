"""LED colour sampling.

Not needed to feed HyperHDR -- it does its own sampling -- but present so the
processor can eventually drive WLED directly and drop HyperHDR entirely.
"""

from processor.led.sampler import LedLayout, LedSampler

__all__ = ["LedLayout", "LedSampler"]
