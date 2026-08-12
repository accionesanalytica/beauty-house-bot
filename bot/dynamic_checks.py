"""Deterministic, read-only execution of Knowledge V1 live requirements.

The caller injects the tool registry. This module neither imports production
integrations nor decides business routing; it only verifies facts that the
governing knowledge topic declared necessary before drafting a response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


SAFE_DYNAMIC_VERIFIERS = {"get_order_status", "get_stock"}


@dataclass(frozen=True)
class DynamicCheckOutcome:
    fact: str
    verifier: str
    status: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    missing_arguments: tuple[str, ...] = ()
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    customer_fallback: str = ""


def execute_dynamic_requirements(
    requirements: Sequence[Any],
    tool_registry: Mapping[str, Callable[..., Mapping[str, Any]]],
) -> tuple[DynamicCheckOutcome, ...]:
    """Run only allowlisted GET-style verifiers whose arguments are complete."""
    outcomes = []
    for requirement in requirements:
        fact = str(getattr(requirement, "fact", ""))
        verifier = str(getattr(requirement, "verifier", ""))
        status = str(getattr(requirement, "status", ""))
        arguments = dict(getattr(requirement, "arguments", {}) or {})
        missing_arguments = tuple(
            getattr(requirement, "missing_arguments", ()) or ()
        )
        customer_fallback = str(getattr(requirement, "customer_fallback", "") or "")
        if status != "ready":
            outcomes.append(DynamicCheckOutcome(
                fact=fact, verifier=verifier, status=status,
                arguments=arguments, missing_arguments=missing_arguments,
                customer_fallback=customer_fallback,
            ))
            continue
        tool = tool_registry.get(verifier)
        if verifier not in SAFE_DYNAMIC_VERIFIERS or tool is None:
            outcomes.append(DynamicCheckOutcome(
                fact=fact, verifier=verifier, status="unavailable_tool",
                arguments=arguments, missing_arguments=missing_arguments,
                customer_fallback=customer_fallback,
            ))
            continue
        try:
            result = tool(**arguments)
            if not isinstance(result, Mapping):
                raise TypeError("La herramienta no devolvió un objeto")
            outcomes.append(DynamicCheckOutcome(
                fact=fact, verifier=verifier, status="completed",
                arguments=arguments, missing_arguments=missing_arguments,
                result=dict(result),
                customer_fallback=customer_fallback,
            ))
        except Exception as error:  # noqa: BLE001
            outcomes.append(DynamicCheckOutcome(
                fact=fact, verifier=verifier, status="failed",
                arguments=arguments, missing_arguments=missing_arguments,
                error=type(error).__name__,
                customer_fallback=customer_fallback,
            ))
    return tuple(outcomes)


def format_dynamic_check_context(outcomes: Sequence[DynamicCheckOutcome]) -> str:
    """Expose verified results and explicit limits to the drafting model."""
    if not outcomes:
        return ""
    lines = ["Verificaciones dinámicas determinísticas de este turno:"]
    for outcome in outcomes:
        if outcome.status == "completed":
            lines.append(
                "- {} verificado con {}: {}".format(
                    outcome.fact,
                    outcome.verifier,
                    json.dumps(outcome.result, ensure_ascii=False, sort_keys=True),
                )
            )
        elif outcome.status == "missing_arguments":
            lines.append("- {} pendiente: faltan argumentos requeridos.".format(outcome.fact))
        elif outcome.status == "unavailable_tool":
            lines.append("- {} no tiene verificador live disponible.".format(outcome.fact))
        else:
            lines.append("- {} no pudo verificarse; no afirmes ese dato.".format(outcome.fact))
    return "\n".join(lines)
