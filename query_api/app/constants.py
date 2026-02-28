"""
Application-wide constants for IMPOSBRO Search API.

Centralizes version, default ports, and validation patterns to avoid
magic numbers and ensure consistency across the codebase.
"""

# Application metadata
APP_NAME = "IMPOSBRO Federated Search API"
VERSION = "4.0.0"

# Typesense defaults
TYPESENSE_DEFAULT_PORT = 8108

# Path parameter validation: alphanumeric, hyphen, underscore (Typesense-compatible)
# Used for collection names and cluster names to prevent injection and invalid identifiers
NAME_PATTERN = r"^[a-zA-Z0-9_-]+$"
