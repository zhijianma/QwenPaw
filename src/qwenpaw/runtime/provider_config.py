# -*- coding: utf-8 -*-
"""Shared validation for generation-pinned provider configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for

_MAX_PROVIDER_CONFIG_BYTES = 64 * 1024


def validate_provider_config(
    capability_id: str,
    config: dict[str, Any],
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and detach one pinned provider configuration snapshot."""
    try:
        encoded = json.dumps(
            config,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"provider '{capability_id}' config must be JSON",
        ) from error
    if len(encoded) > _MAX_PROVIDER_CONFIG_BYTES:
        raise ValueError(f"provider '{capability_id}' config is too large")
    if config and schema is None:
        raise ValueError(
            f"provider '{capability_id}' does not declare config_schema",
        )
    if schema is not None:
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validation_error = best_match(
                validator_class(schema).iter_errors(config),
            )
        except SchemaError as schema_error:
            raise ValueError(
                f"provider '{capability_id}' has an invalid config_schema",
            ) from schema_error
        if validation_error is not None:
            location = ".".join(
                str(item) for item in validation_error.absolute_path
            )
            suffix = f" at '{location}'" if location else ""
            raise ValueError(
                f"provider '{capability_id}' config does not match "
                f"config_schema{suffix}",
            )
    return deepcopy(config)


__all__ = ["validate_provider_config"]
