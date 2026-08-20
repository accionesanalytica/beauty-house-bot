# Fred v1 vs v2 — evaluación A/B offline

Fecha de ejecución: `2026-08-20T03:28:00Z`. Casos: **54**. Modelo real: **sí**.

## Recomendación

**GO** — V2 no tuvo bloqueantes y todos los handoffs de evaluación fueron side-effect-free.

Esta ejecución usa el LLM real con fixtures deterministas de Knowledge/Tiendanube. No lee pedidos reales, no envía WhatsApp, no crea handoffs ni checkout y no modifica estado productivo.

## Resultado general

| Métrica | v1 | v2 |
|---|---:|---:|
| PASS | 28 | 50 |
| REVIEW | 18 | 4 |
| FAIL | 8 | 0 |
| Win rate pareado | 0.0% | 40.7% |
| Latencia p50 | 1743.46 ms | 2853.77 ms |
| Latencia p95 | 10342.00 ms | 4156.22 ms |
| Tool calls promedio | 1.259 | 0.778 |
| LLM calls promedio | 1.944 | 1.778 |
| Tokens promedio | 10408.63 | 2929.76 |

Empates: **32**.

## Etapa 2.1: anterior vs nuevo

| Resultado v2 | Antes | Nuevo |
|---|---:|---:|
| PASS | 39 | 50 |
| REVIEW | 6 | 4 |
| FAIL | 9 | 0 |

Cambios por blocker:

- **Product advice:** prompt y contrato de `handoff_to_isa` obligan `reason=product_advice`; no se consulta catálogo.
- **Product not found:** `get_product` devuelve `status=not_found`, única transición `custom_order` y una respuesta segura de encargo, sin sustitutos ni búsqueda circular.
- **Stale context:** el turno actual se separa del historial con prioridad explícita; cortesías no reutilizan el tema anterior. El FAIL `topic-03` original había llamado Knowledge correctamente: el handoff extra provenía del texto recuperado, no de `active_product`.
- **PACKED:** el adapter reutiliza el mapping auditado de v1 y entrega `fulfillment_semantics` + `customer_safe_reply`; el modelo no puede convertir PACKED en listo para retirar.
- **Dry-run handoff:** evaluación/shadow devuelve `status=simulated_success`, `would_handoff=true`, `side_effect_executed=false`; la respuesta representa producción sin ejecutar el efecto.

## Fallos por causa

- v1: `{"missing_data": 16, "stale_context": 1, "unnecessary_tool": 19, "wrong_handoff": 5, "wrong_tool": 3}`
- v2: `{"missing_data": 3, "unnecessary_tool": 1}`

- FAIL v1 (8): `topic-04, policy-01, policy-03, product-06, advice-01, advice-02, advice-03, ambiguous-01`
- FAIL v2 (0): ``

## Bloqueantes v2

| Control | Resultado |
|---|---:|
| Hallucination flags | 0 |
| Checkout creado/propuesto | 0 |
| Casos de asesoría sin handoff correcto | 0 |
| Producto inexistente sin handoff correcto | 0 |
| Cambio de tema con estado viejo | 0 |
| Handoffs shadow con side effect | 0 |

## 10 peores casos de v2

| Caso | Estado | Causas | Respuesta v2 | Tools v2 |
|---|---|---|---|---|
| order-07 | REVIEW | missing_data | Tu pedido #6342 ya está empaquetado. Antes de acercarte, esperá la confirmación por correo así no hacés el viaje en vano. | get_order |
| order-10 | REVIEW | missing_data | Tu pedido #6342 ya está empaquetado. Antes de acercarte, esperá la confirmación por correo así no hacés el viaje en vano. | get_order |
| order-12 | REVIEW | missing_data | Tu pedido #6342 ya está empaquetado. Antes de acercarte, esperá la confirmación por correo así no hacés el viaje en vano. | get_order |
| wholesale-06 | REVIEW | unnecessary_tool | ¡Listo! Te dejé con Isa para que te pase la lista y los precios mayoristas de SHOOW TOOLS. Ella te va a confirmar todo por acá. 😊 | search_knowledge, handoff_to_isa |
| social-01 | PASS | - | ¡Hola! ¿Cómo andás? ¿En qué te puedo ayudar hoy? 😊 | ninguna |
| social-02 | PASS | - | ¡Hola! 👋 ¿Cómo andás? ¿En qué te puedo ayudar hoy? | ninguna |
| social-03 | PASS | - | ¡Hola! 👋 ¿En qué te puedo ayudar hoy? | ninguna |
| social-04 | PASS | - | ¡Hola! 👋 ¿En qué te puedo ayudar hoy? | ninguna |
| social-05 | PASS | - | ¡De nada! Cualquier cosa me avisás 😊 | ninguna |
| social-06 | PASS | - | ¡Genial! ¿Querés que te pase los horarios y cómo coordinar la visita al showroom? | ninguna |

## Soporte shadow preparado

`bot/v2_shadow.py` recibe el mismo turno/historial después de v1 y devuelve únicamente respuesta propuesta, tools, latencia, decisión, llamadas/tokens y errores. Usa el resultado estructurado `simulated_success` de v2; no conoce teléfono, no envía WhatsApp, no crea checkout y no modifica estado. `bot/app.py` no lo importa todavía.

## Verificación

- Suite completa: **691 tests OK** (661 originales de v1 + 30 de v2/A-B).
- Modelo real: DeepSeek; datos comerciales: fixtures locales deterministas.
- Sin llamadas a Meta, Railway, Supabase o Tiendanube y sin escrituras externas.

## Alcance del veredicto

El resultado habilita o bloquea únicamente el siguiente paso: preparar un shadow real read-only. No autoriza conectar el webhook, desplegar, enviar respuestas v2 ni escribir en sistemas productivos.

El JSON adjunto conserva por caso: respuestas, tools/argumentos, llamadas LLM, tokens, latencia, errores, handoff, datos live simulados, hallucination flags, estado y causas.
