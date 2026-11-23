"""Sub-pixel survey mosaic across archived visits.

Public entry point:

* render(specification) -> schema_version=1 mosaic report

Archives are only read; the archive format, the existing entry points and
their outputs are untouched.  All geometry, integration and error propagation
are exact rational and the package uses only the Python standard library.
"""
from .model import MosaicError
from .render import render

__all__ = ["render", "MosaicError"]
