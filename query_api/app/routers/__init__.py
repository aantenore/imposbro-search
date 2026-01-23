"""
Routers package for IMPOSBRO Search API.

This package contains all API route definitions organized by domain.
"""

from .admin import router as admin_router
from .search import router as search_router

__all__ = [
    "admin_router",
    "search_router",
]
