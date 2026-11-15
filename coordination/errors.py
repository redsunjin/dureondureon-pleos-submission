"""Shared API errors/identifiers for reservation and simulated curb sessions."""
import re

IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ApiError(400, 'invalid_identifier')
    return value
