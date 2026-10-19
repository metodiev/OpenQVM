"""OpenQVM - an open-source desktop virtualization platform."""

from __future__ import annotations

__all__ = ["PRODUCT_NAME", "PRODUCT_VERSION", "USER_AGENT", "__version__"]

PRODUCT_NAME = "OpenQVM"
__version__ = "0.1.0"
PRODUCT_VERSION = __version__
USER_AGENT = f"{PRODUCT_NAME}/{PRODUCT_VERSION}"
