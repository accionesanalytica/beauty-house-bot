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
from evaluation import assess_case  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Ejecuta llamadas de solo lectura a DeepSeek/Tiendanube.")
    parser.add_argument("--limit", type=int, default=len(CURATED_CASES))
    parser.add_argument("--start", type=int, default=0, help="Índice inicial de caso para continuar una muestra.")
    parser.add_argument("--allow-large-batch", action="store_true", help="Permite más de 10 casos live; usa más APIs.")
    args = parser.parse_args()

    cases = CURATED_CASES[max(0, args.start): max(0, args.start) + max(0, args.limit)]
    if not args.live:
        print("Modo seguro: {} casos listos. Usá --live para evaluarlos sin enviar WhatsApps.".format(len(cases)))
        return
    if len(cases) > 10 and not args.allow_large_batch:
        parser.error("Modo live limitado a 10 casos. Usá --allow-large-batch sólo después de revisar costo y alcance.")

    attention_required = 0
    scores = []
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
        assessment = assess_case(case, result)
        scores.append(assessment["score"])
        findings = assessment["findings"]
        print("Decisión: {} | Puntaje automático: {}/100".format(assessment["action"], assessment["score"]))
        if findings:
            attention_required += 1
            print("REVISAR: {}".format(" | ".join(item["message"] for item in findings)))
        else:
            print("OK automático — validar tono según: {}".format(case.notes))

    average = sum(scores) / len(scores) if scores else 0
    print("\nResultado: {} caso(s) requieren revisión humana. Puntaje promedio: {:.1f}/100.".format(attention_required, average))


if __name__ == "__main__":
    main()
