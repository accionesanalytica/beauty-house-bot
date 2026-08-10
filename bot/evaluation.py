"""Reusable, deterministic rubric for Fred evaluations.

It never decides whether a reply is commercially beautiful. It catches the
objective failures that should stop a deployment and leaves tone/advisory fit
as explicit human review.
"""

from typing import Any, Dict, List


CRITICAL_PENALTY = 45
MAJOR_PENALTY = 20


def assess_case(case: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    """Assess one live-agent result against a curated case, without APIs."""
    reply = str(result.get("reply") or "")
    reply_lower = reply.lower()
    decision = result.get("decision") or {}
    action = str(decision.get("action") or "reply")
    tool_names = [call.get("name") for call in result.get("tool_calls", [])]
    findings: List[Dict[str, str]] = []

    for forbidden in getattr(case, "forbidden_fragments", ()):
        if forbidden.lower() in reply_lower:
            findings.append({"severity": "critical", "message": "Incluyó texto prohibido: {}".format(forbidden)})

    required_any = getattr(case, "required_any_fragments", ())
    if required_any and not any(fragment.lower() in reply_lower for fragment in required_any):
        findings.append({
            "severity": "major",
            "message": "No pidió la precisión esperada: {}.".format(" / ".join(required_any)),
        })

    if "http" in reply_lower and "get_product_availability" not in tool_names:
        findings.append({"severity": "critical", "message": "Incluyó link sin URL de producto verificada."})

    if getattr(case, "should_escalate", False) and action != "handoff_to_isa":
        findings.append({"severity": "critical", "message": "Caso sensible sin escalación estructurada a Isa."})

    required_tool = getattr(case, "required_tool", "")
    if required_tool and required_tool not in tool_names:
        findings.append({"severity": "major", "message": "No usó la verificación requerida: {}.".format(required_tool)})

    expected_action = getattr(case, "expected_action", "")
    if expected_action and action != expected_action:
        findings.append({"severity": "major", "message": "Acción {} en vez de {}.".format(action, expected_action)})

    score = 100
    for finding in findings:
        score -= CRITICAL_PENALTY if finding["severity"] == "critical" else MAJOR_PENALTY
    return {
        "case_id": getattr(case, "case_id", "unknown"),
        "score": max(score, 0),
        "action": action,
        "findings": findings,
        "human_review": getattr(case, "notes", ""),
    }
