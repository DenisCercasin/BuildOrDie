"""Merchant intelligence primitives used by the BookPool backend.

The core package deliberately has no web-framework dependency.  It can be
called directly by the optimiser or exposed through the FastAPI adapter in
``merchant_intelligence.api``.
"""

from .service import MerchantIntelligenceService, create_demo_service

__all__ = ["MerchantIntelligenceService", "create_demo_service"]
