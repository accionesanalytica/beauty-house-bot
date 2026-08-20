# Fred v1 vs v2 — evaluación A/B offline

Fecha de ejecución: `2026-08-20T02:41:52Z`. Casos: **54**. Modelo real: **sí**.

## Recomendación

**NO-GO** — V2 conserva fallos bloqueantes o no alcanza 90% PASS; no ejecutar shadow real todavía.

Esta ejecución usa el LLM real con fixtures deterministas de Knowledge/Tiendanube. No lee pedidos reales, no envía WhatsApp, no crea handoffs ni checkout y no modifica estado productivo.

## Resultado general

| Métrica | v1 | v2 |
|---|---:|---:|
| PASS | 32 | 39 |
| REVIEW | 16 | 6 |
| FAIL | 6 | 9 |
| Win rate pareado | 20.4% | 31.5% |
| Latencia p50 | 1966.93 ms | 3115.95 ms |
| Latencia p95 | 10959.71 ms | 4815.13 ms |
| Tool calls promedio | 1.148 | 0.833 |
| LLM calls promedio | 1.926 | 1.815 |
| Tokens promedio | 10254.56 | 2016.19 |

Empates: **26**.

## Fallos por causa

- v1: `{"missing_data": 16, "stale_context": 1, "unnecessary_tool": 18, "wrong_handoff": 3, "wrong_tool": 4}`
- v2: `{"hallucination": 6, "missing_data": 4, "stale_context": 1, "unnecessary_tool": 6, "wrong_handoff": 3, "wrong_tool": 3}`

- FAIL v1 (6): `topic-04, policy-01, policy-03, product-06, wholesale-06, ambiguous-01`
- FAIL v2 (9): `topic-03, order-07, product-06, purchase-02, purchase-04, advice-01, advice-02, advice-03, wholesale-06`

## Bloqueantes v2

| Control | Resultado |
|---|---:|
| Hallucination flags | 6 |
| Checkout creado/propuesto | 0 |
| Casos de asesoría sin handoff correcto | 3 |
| Producto inexistente sin handoff correcto | 1 |
| Cambio de tema con estado viejo | 1 |

## 10 peores casos de v2

| Caso | Estado | Causas | Respuesta v2 | Tools v2 |
|---|---|---|---|---|
| advice-03 | FAIL | wrong_tool, wrong_handoff, missing_data | ¡Hola! Para asesorarte bien sobre qué pestañas te convienen para lifting, necesito saber un poquito más. ¿Tenés alguna marca o tipo en particular en mente, o querés que te recomien | ninguna |
| topic-03 | FAIL | stale_context, hallucination | Te paso con Isa que te va a confirmar la lista y los precios mayoristas. ¡Quedate tranqui que te atiende enseguida! 😊 | search_knowledge, handoff_to_isa |
| order-07 | FAIL | missing_data, hallucination | ¡Hola! Te cuento sobre tu pedido 6342:  ✅ **Pago confirmado** 📦 **Estado: empacado** — ya está listo para retirar.  Como elegiste **retiro en el showroom**, no hay envío ni trackin | get_order |
| product-06 | FAIL | wrong_tool, wrong_handoff | Uy, no encontré nada con ese nombre exacto en nuestro catálogo 😕. ¿Tenés alguna otra referencia o marca de las pestañas que buscás? Así te ayudo a ver si las tenemos o te conecto c | get_product |
| purchase-04 | FAIL | unnecessary_tool, hallucination | ¡Listo! Isa te va a poder ayudar con la compra de tu Isabel I en Chocolate. Te va a contactar para coordinar el pago y el envío. 😊 ¿Necesitás algo más? | get_product, handoff_to_isa |
| advice-01 | FAIL | wrong_tool, wrong_handoff | ¡Hola! 😊 Qué lindo que quieras pestañas naturales. Para poder asesorarte bien, contame un poco más: ¿buscás un servicio de pestañas (como lifting o extensiones) o querés comprar al | ninguna |
| purchase-02 | FAIL | hallucination | ¡Listo! Isa te va a ayudar con la compra de las dos unidades de Isabel I Chocolate. Quedate tranqui que te va a contactar para coordinar todo. 😊 | handoff_to_isa |
| advice-02 | FAIL | hallucination | Te paso con Isa para que te asesore y elijamos juntas el modelo ideal para vos. 😊 ¿Algo más que necesites mientras tanto? | handoff_to_isa |
| wholesale-06 | FAIL | hallucination | Listo, te paso con Isa que te va a asesorar con la compra mayorista de SHOOW TOOLS. Ella te va a dar una mano con todo el tema. 😊 | handoff_to_isa |
| ambiguous-07 | REVIEW | unnecessary_tool, missing_data | ¡Sí! Tenemos pestañas marrones (chocolate) en el catálogo:  - **SHOOW TOOLS - ISABEL I** en Chocolate — $12.500, hay stock ✅ - **SHOOW TOOLS - TAYLOR** en Chocolate — $11.900, pero | get_product |

## Soporte shadow preparado

`bot/v2_shadow.py` recibe el mismo turno/historial después de v1 y devuelve únicamente respuesta propuesta, tools, latencia, decisión, llamadas/tokens y errores. Usa el handoff preview/dry-run de v2; no conoce teléfono, no envía WhatsApp, no crea checkout y no modifica estado. `bot/app.py` no lo importa todavía.

## Verificación

- Suite completa: **683 tests OK** (661 originales de v1 + 22 de v2/A-B).
- Modelo real: DeepSeek; datos comerciales: fixtures locales deterministas.
- Sin llamadas a Meta, Railway, Supabase o Tiendanube y sin escrituras externas.

## Alcance del veredicto

El resultado habilita o bloquea únicamente el siguiente paso: preparar un shadow real read-only. No autoriza conectar el webhook, desplegar, enviar respuestas v2 ni escribir en sistemas productivos.

El JSON adjunto conserva por caso: respuestas, tools/argumentos, llamadas LLM, tokens, latencia, errores, handoff, datos live simulados, hallucination flags, estado y causas.
