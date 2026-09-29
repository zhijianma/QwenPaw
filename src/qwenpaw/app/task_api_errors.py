# -*- coding: utf-8 -*-
"""Stable failures shared by task routes and exception handlers."""


class TaskApiError(Exception):
    """Stable task API failure rendered as an RFC 9457 response."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)
