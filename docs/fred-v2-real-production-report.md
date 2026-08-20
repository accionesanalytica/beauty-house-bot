# Fred v2 — cuatro casos reales prioritarios

Fecha: `2026-08-20`. Rama: `fred-v2-agent`. Ejecución offline con el modelo real y adapters comerciales reemplazados por fixtures read-only/dry-run.

## Resultado

V2 obtuvo **4/4 PASS**, sin bloqueantes, alucinaciones, checkout ni side effects. V1 obtuvo **1/4 PASS**. El veredicto para preparar el PR de shadow desactivado es **GO**; esto no autoriza deploy ni activación en Railway.

| Caso | Respuesta v2 | Tools | LLM calls | Tokens | Latencia | Estado |
|---|---|---:|---:|---:|---:|---:|
| `ok gracias!` después de showroom | `¡De nada! Cualquier cosa que necesites, acá estoy. 😊` | ninguna | 1 | 1.759 | 1.414,78 ms | PASS |
| `con que mas me puedes ayudar?` | Describe showroom/políticas, pedidos, productos y derivación a Isa; no afirma datos live | ninguna | 1 | 1.842 | 2.044,01 ms | PASS |
| `cancelar pedido` | Informa que deriva a Isa para gestionar la cancelación | `handoff_to_isa(reason=human_request)` | 2 | 3.694 | 2.690,44 ms | PASS |
| showroom + `podria hablar con isa directamente?` | Prioriza la derivación a Isa | `handoff_to_isa(reason=human_request)` | 2 | 3.717 | 2.636,70 ms | PASS |

Los dos handoffs devolvieron `status=simulated_success`, `would_handoff=true` y `side_effect_executed=false`. No se consultó Knowledge, catálogo ni Tiendanube en estos cuatro casos.

## Antes y después

En la reproducción inicial, v2 tuvo 2 PASS y 2 FAIL según el evaluador. Uno era real: `cancelar pedido` pedía el número en vez de derivar. El otro era un falso positivo: la descripción de la capacidad de consultar disponibilidad fue marcada como afirmación de stock. Corregido el contrato del agente/tool y el auditor, la repetición exacta quedó en 4 PASS y 0 FAIL.

No se agregaron regex ni routing determinista. Los cambios fueron:

- Instrucción semántica: cancelar, cambiar, devolver o intervenir un pedido real es una acción sensible y deriva directamente a Isa.
- Prioridad semántica: una solicitud explícita de persona/Isa domina aunque el mismo turno también contenga otra consulta.
- Contrato de capacidades: responder brevemente sin tools y distinguir capacidades de resultados live.
- Descripción de `handoff_to_isa`: `human_request` cubre solicitudes humanas y acciones sensibles sobre pedidos, sin consultar otras tools primero.
- Evaluador: tools estrictas, límite de llamadas LLM y auditoría que no confunde una capacidad declarada con stock afirmado.

## Comparación

| Métrica | v1 | v2 |
|---|---:|---:|
| PASS / FAIL | 1 / 3 | 4 / 0 |
| Win rate pareado | 0% | 75% |
| Empates | 1 | 1 |
| Latencia p50 | 1.448,87 ms | 2.044,01 ms |
| Latencia p95 | 4.276,79 ms | 2.690,44 ms |
| Tools promedio | 1,25 | 0,50 |
| LLM calls promedio | 1,25 | 1,50 |
| Tokens promedio | 6.455,25 | 2.753,00 |

## Verificación y seguridad

- Suite completa: **709/709 tests OK**.
- `FRED_V2_SHADOW_ENABLED=false` continúa siendo el default.
- Sin envío de WhatsApp, cancelación de pedidos, checkout, escritura de estado, deploy ni cambios en Railway.
- Evidencia cruda: `evals/results/fred_v2_real_production_baseline.json` y `evals/results/fred_v2_real_production_latest.json`.
