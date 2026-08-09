"""Optional live evaluation runner for Fred.

Usage (read-only APIs; never sends WhatsApp):
    python tests/run_fred_live_evals.py --live --limit 10

This is intentionally not part of the default test suite because it calls
DeepSeek and Tiendanube, so responses/costs and catalog data change over time.
"""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import answer  # noqa: E402
from fred_eval_cases import CURATED_CASES  # noqa: E402


def _critical_findings(case, result):
    reply = (result.get("reply") or "").lower()
    findings = []
    for forbidden in case.forbidden_fragments:
        if forbidden.lower() in reply:
            findings.append("Incluyó texto prohibido: {}".format(forbidden))
    if "http" in reply and not any(
        call.get("name") == "get_product_availability"
        for call in result.get("tool_calls", [])
    ):
        findings.append("Incluyó link sin haber obtenido URL de producto verificada.")
    if case.should_escalate and not result.get("handoff") and "isa" not in reply:
        findings.append("Caso sensible: revisar si debió escalar a Isa.")
    if case.required_tool and not any(
        call.get("name") == case.required_tool
        for call in result.get("tool_calls", [])
    ):
        findings.append("No usó la verificación requerida: {}.".format(case.required_tool))
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Ejecuta llamadas de solo lectura a DeepSeek/Tiendanube.")
    parser.add_argument("--limit", type=int, default=len(CURATED_CASES))
    parser.add_argument("--start", type=int, default=0, help="Índice inicial de caso para continuar una muestra.")
    args = parser.parse_args()

    cases = CURATED_CASES[max(0, args.start): max(0, args.start) + max(0, args.limit)]
    if not args.live:
        print("Modo seguro: {} casos listos. Usá --live para evaluarlos sin enviar WhatsApps.".format(len(cases)))
        return

    attention_required = 0
    for case in cases:
        print("\n[{}] {}".format(case.case_id, case.customer_message))
        try:
            result = answer(
                case.customer_message,
                history=[],
                greeting_required=True,
                verbose=False,
            )
        except Exception as error:  # noqa: BLE001
            attention_required += 1
            print("ERROR: {}".format(type(error).__name__))
            continue

        print("Fred: {}".format(result.get("reply", "").replace("\n", " ")))
        findings = _critical_findings(case, result)
        if findings:
            attention_required += 1
            print("REVISAR: {}".format(" | ".join(findings)))
        else:
            print("OK automático — validar tono según: {}".format(case.notes))

    print("\nResultado: {} caso(s) requieren revisión humana.".format(attention_required))


if __name__ == "__main__":
    main()
