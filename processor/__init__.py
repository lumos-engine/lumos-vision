"""TV Vision Processor.

Takes a camera feed pointed at a TV and turns it into a rectified, cropped,
low-latency video stream suitable for ambient lighting systems.

The package deliberately knows nothing about HyperHDR, WLED or any other
consumer: it produces frames and hands them to pluggable output sinks.
"""

__version__ = "0.1.0"
