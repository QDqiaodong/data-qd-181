"""Spatial background modelling and background-subtracted delivery.

Public entry point:

* render(path, revision, specification) -> schema_version=4 report

Only one already-committed archive revision is read; the archive format, fact
writes and discovery messages are untouched.  All arithmetic is exact rational
and the package uses only the Python standard library.
"""
from .model import SkyError, SkyInconsistent
from .render import render

__all__ = ["render", "SkyError", "SkyInconsistent"]
