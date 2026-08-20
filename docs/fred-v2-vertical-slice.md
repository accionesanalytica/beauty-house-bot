# Fred v2 — reporte del primer vertical slice

Referencia: `fred-v2-agent`, sin conexión al webhook productivo, sin deploy y sin
cambios de configuración o datos externos.

## Archivos

### Nuevos

- `bot/v2_agent.py`: único agente semántico v2 y loop de function calling.
- `bot/v2_tools.py`: superficie cerrada y adaptadores de las cuatro tools.
- `evals/fred_v2_ab.py`: harness side-effect-free para comparar turnos v1/v2.
- `tests/test_v2_agent.py`: contrato, siete casos y harness A/B.
- `docs/fred-v2-vertical-slice.md`: este reporte.

### Modificado

- `bot/tiendanube_tools.py`: `get_product_availability()` ahora expone el precio
  que ya devuelve Tiendanube. Es un campo aditivo; evita duplicar una consulta o
  una integración para v2.

No se modificaron `bot/app.py`, el webhook, Railway, Supabase, Meta ni checkout.

## Arquitectura resultante

```text
mensaje + hasta 8 mensajes de contexto
              ↓
        FredV2Agent (un LLM)
              ↓ elige 0..n tools, máximo 4 ejecuciones
  ┌──────────────┬───────────┬─────────────┬────────────────┐
  │search_knowledge│ get_order │ get_product │ handoff_to_isa │
  └──────┬───────┴─────┬─────┴──────┬──────┴────────┬───────┘
         ↓             ↓            ↓               ↓
 Knowledge v1   Tiendanube order  catálogo +     preview seguro
 local/Supabase fulfillments[]    Tiendanube live (live opt-in futuro)
```

El LLM decide significado, continuidad y herramienta. Los wrappers sólo validan
argumentos, identificadores, evidencia live, handoff y límites. `app.py` no llama
a v2: hoy sólo se puede ejecutar explícitamente desde código/tests/harness.

## Siete casos del slice

| Mensaje/contexto | Respuesta de ejemplo | Tools |
|---|---|---|
| `Hooolaaa` / `Hello there` | `¡Hooolaaa! 😊 ¿Cómo te puedo ayudar?` | ninguna |
| `quiero pasar por el showroom` | `Podés pasar por el showroom coordinando previamente 😊` | `search_knowledge` |
| `quiero saber dónde está mi pedido` | `Claro, ¿me pasás el número de pedido?` | ninguna |
| después: `6344` | `El pedido #6344 está preparado para retiro.` | `get_order(6344)`; evidencia `fulfillment_status=PACKED`, `shipping_type=pickup` |
| `¿Tienen Isabel I Chocolate?` | `Sí, Isabel I Chocolate figura disponible.` | `get_product` |
| `quiero 4 Isabel I Chocolate` | `¡Genial! Isa puede ayudarte a coordinar la compra.` | `handoff_to_isa(reason=purchase_intent)`; preview, sin checkout |
| `quiero unas pestañas naturales` | `Para recomendarte bien, Isa puede ayudarte 😊` | `handoff_to_isa(reason=product_advice)`; preview |

Son ejemplos deterministas de tests con modelo y proveedores simulados: prueban
la orquestación, no pretenden certificar todavía la redacción de DeepSeek real.

## Harness A/B

`compare_turns()` entrega por mensaje y versión:

- respuesta;
- tools y argumentos;
- evidencia devuelta por tools;
- número de llamadas LLM;
- latencia;
- errores;
- hallucinations (hoy: URLs visibles no respaldadas por evidencia; la corrección
  factual más amplia queda explícitamente para el rubric humano/evals).

Cada versión conserva su propio historial, pero recibe la misma secuencia de
mensajes. Los runners se inyectan para que usar servicios reales sea una decisión
explícita y el harness no mande WhatsApp ni escriba sistemas por accidente.

## Tests

```text
Ran 673 tests in 3.497s
OK
```

- 661 tests previos de v1: verdes.
- 12 tests nuevos de v2: verdes.
- Suite offline: envíos reales bloqueados y proveedores simulados.
- Advertencias conocidas: Python 3.9 fuera de soporte y LibreSSL; sin fallas.

## Routing viejo no necesario en este slice

V2 no importó ni ejecutó para comprender estos casos:

- `routing_policy.py` y su clasificación previa de intención/data requirement;
- regex de saludos, intención de compra, asesoramiento o tracking;
- detección heurística de producto nombrado en `app.py`;
- `FredCore` y sus modos `CHAT/MENU/TRACKING/CHECKOUT`;
- pre-routes sociales sin LLM;
- correcciones post-model que reemplazan la decisión semántica;
- `set_turn_decision`, `select_sale_candidate` y las tools internas de v1;
- flujo de sales intake y creación de checkout.

Sí se conservaron límites deterministas de tools, validación de order number,
identidad/SKU y evidencia live de Tiendanube, Knowledge aprobado y el adaptador
existente de cola/WhatsApp/estado para un handoff live futuro y explícito.

## Límite de esta etapa

No se hizo una prueba live con DeepSeek ni una comparación A/B real contra el
pipeline completo de v1. Eso pertenece a la segunda etapa (replay/shadow) y no
debe empezar sin revisar este corte.
