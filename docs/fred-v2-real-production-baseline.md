# Fred v1 vs v2 — evaluación A/B offline

Fecha de ejecución: `2026-08-20T22:33:43Z`. Casos: **4**. Modelo real: **sí**.

## Recomendación

**NO-GO** — V2 conserva fallos bloqueantes o un handoff de evaluación ejecutó side effects; no ejecutar shadow real todavía.

Esta ejecución usa el LLM real con fixtures deterministas de Knowledge/Tiendanube. No lee pedidos reales, no envía WhatsApp, no crea handoffs ni checkout y no modifica estado productivo.

## Resultado general

| Métrica | v1 | v2 |
|---|---:|---:|
| PASS | 1 | 2 |
| REVIEW | 0 | 0 |
| FAIL | 3 | 2 |
| Win rate pareado | 0.0% | 25.0% |
| Latencia p50 | 1690.24 ms | 1211.95 ms |
| Latencia p95 | 3766.83 ms | 3244.85 ms |
| Tool calls promedio | 1.250 | 0.250 |
| LLM calls promedio | 1.250 | 1.250 |
| Tokens promedio | 6453.50 | 1997.00 |

Empates: **3**.

## Etapa 2.1: anterior vs nuevo

| Resultado v2 | Antes | Nuevo |
|---|---:|---:|
| PASS | 39 | 2 |
| REVIEW | 6 | 0 |
| FAIL | 9 | 2 |

Cambios por blocker:

- **Product advice:** prompt y contrato de `handoff_to_isa` obligan `reason=product_advice`; no se consulta catálogo.
- **Product not found:** `get_product` devuelve `status=not_found`, única transición `custom_order` y una respuesta segura de encargo, sin sustitutos ni búsqueda circular.
- **Stale context:** el turno actual se separa del historial con prioridad explícita; cortesías no reutilizan el tema anterior. El FAIL `topic-03` original había llamado Knowledge correctamente: el handoff extra provenía del texto recuperado, no de `active_product`.
- **PACKED:** el adapter reutiliza el mapping auditado de v1 y entrega `fulfillment_semantics` + `customer_safe_reply`; el modelo no puede convertir PACKED en listo para retirar.
- **Dry-run handoff:** evaluación/shadow devuelve `status=simulated_success`, `would_handoff=true`, `side_effect_executed=false`; la respuesta representa producción sin ejecutar el efecto.

## Fallos por causa

- v1: `{"wrong_handoff": 1, "wrong_tool": 3}`
- v2: `{"hallucination": 1, "wrong_handoff": 1, "wrong_tool": 1}`

- FAIL v1 (3): `real-prod-02, real-prod-03, real-prod-04`
- FAIL v2 (2): `real-prod-02, real-prod-03`

## Bloqueantes v2

| Control | Resultado |
|---|---:|
| Hallucination flags | 1 |
| Checkout creado/propuesto | 0 |
| Casos de asesoría sin handoff correcto | 0 |
| Producto inexistente sin handoff correcto | 0 |
| Cambio de tema con estado viejo | 0 |
| Handoffs shadow con side effect | 0 |

## 10 peores casos de v2

| Caso | Estado | Causas | Respuesta v2 | Tools v2 |
|---|---|---|---|---|
| real-prod-03 | FAIL | wrong_tool, wrong_handoff | ¡Hola! Claro, te ayudo con eso. Para cancelar tu pedido necesito el número del mismo. ¿Me lo pasás? 😊 | ninguna |
| real-prod-02 | FAIL | hallucination | ¡Hola! 😊 Soy Fred, el asistente de Beauty House. Puedo ayudarte con varias cosas:  - **Consultar el estado de tu pedido** (con el número de pedido) - **Verificar si un producto est | ninguna |
| real-prod-01 | PASS | - | ¡De nada! Cualquier cosa que necesites, acá estoy. 😊 | ninguna |
| real-prod-04 | PASS | - | ¡Listo! Ya te dejé anotada con Isa para coordinar la visita al showroom. Ella te va a estar escribiendo en breve para acordar día y horario. 😊  ¿Necesitás algo más mientras tanto? | handoff_to_isa |

## Soporte shadow preparado

`bot/v2_shadow.py` recibe el mismo turno/historial después de v1 y devuelve únicamente respuesta propuesta, tools, latencia, decisión, llamadas/tokens y errores. Usa el resultado estructurado `simulated_success` de v2; no conoce teléfono, no envía WhatsApp, no crea checkout y no modifica estado. `bot/app.py` no lo importa todavía.

## Verificación

- Suite completa: **691 tests OK** (661 originales de v1 + 30 de v2/A-B).
- Modelo real: DeepSeek; datos comerciales: fixtures locales deterministas.
- Sin llamadas a Meta, Railway, Supabase o Tiendanube y sin escrituras externas.

## Alcance del veredicto

El resultado habilita o bloquea únicamente el siguiente paso: preparar un shadow real read-only. No autoriza conectar el webhook, desplegar, enviar respuestas v2 ni escribir en sistemas productivos.

El JSON adjunto conserva por caso: respuestas, tools/argumentos, llamadas LLM, tokens, latencia, errores, handoff, datos live simulados, hallucination flags, estado y causas.
