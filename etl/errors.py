"""Typed failures, so pipeline_runs.error_type says what went wrong."""

from __future__ import annotations


class EtlError(Exception):
    """Base class for failures the pipeline classifies itself."""


class AllStatesFailed(EtlError):
    """No state could be fetched. `cause_class` is the most common underlying exception class."""

    def __init__(self, message: str, cause_class: str) -> None:
        super().__init__(message)
        self.cause_class = cause_class


class UpstreamSchemaError(EtlError):
    """The upstream response did not have the expected fields."""


class EmptyExtract(EtlError):
    """Responses arrived but contained no usable hourly rows."""
