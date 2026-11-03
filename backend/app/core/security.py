"""Security utilities — authentication, authorization, and cryptographic helpers will be implemented in Phase 2."""


def sanitize_input(value: str) -> str:
    """Strip leading and trailing whitespace from *value*."""
    return value.strip()
