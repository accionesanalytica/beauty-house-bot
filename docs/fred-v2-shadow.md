# Fred v2 — shadow real read-only

Estado: implementado detrás de feature flag, **apagado por defecto**. Este
documento no autoriza un deploy ni una activación.

## Punto de enganche

`bot/app.py` arma un sobre inmutable después de validar que la conversación
sigue en estado `BOT`. La ejecución v2 todavía no empieza. Recién cuando el
sender confirma que la respuesta v1 fue entregada, `_observe_v2_shadow_delivery`
envía el trabajo a un executor acotado y devuelve inmediatamente.

Esto cubre respuestas de texto y los dos sender de botones para clientas. V1
sigue siendo el único que envía y persiste estado operativo. Si el flag está
apagado, los helpers retornan antes de importar el runtime v2.

## Aislamiento y fail-open

- `ShadowReadOnlyTools` expone sólo `search_knowledge`, `get_order`,
  `get_product` y `handoff_to_isa`.
- Knowledge, catálogo, stock/precio y pedido reutilizan los adapters existentes
  de lectura.
- El handoff siempre devuelve `status=simulated_success`,
  `would_handoff=true`, `side_effect_executed=false`.
- El adapter live con acceso a `_queue_for_isa` vive separado en
  `bot/v2_live_adapters.py`; shadow no lo importa y rechaza inyectar otro
  handoff.
- Cualquier tool fuera de la lista o resultado que declare side effects falla.
- El trabajo tiene deadline propio, dos workers y ocho slots pendientes. Timeout,
  excepción, error de modelo/tool/DB o cola llena se registran y se abandonan;
  ninguno propaga al sender v1.

## Observabilidad

La salida de aplicación contiene una línea sin texto completo ni teléfono:

```text
[FredShadow] correlation_id=... conversation_id=... generation=... v1_response_hash=... v2_response_hash=... v2_tools=... v2_llm_calls=... v2_tokens=... v2_latency_ms=... v2_handoff_reason=... v2_error=... side_effects=false
```

La tabla aislada `fred_v2_shadow_observations` guarda copias con teléfono/email
redactados, hashes, tool calls/results, métricas y error. No escribe en
`conversations`, `messages`, Fred Core, ownership ni decisiones de v1.

El SQL versionado está en `docs/sql/011_fred_v2_shadow_observations.sql`.

## Evaluación offline

Exportar la tabla como JSON o JSONL y ejecutar:

```bash
python evals/run_fred_v2_shadow_eval.py --input shadow.json --output shadow-report.json
```

El reporte incluye total, PASS/REVIEW/FAIL, blockers, win rate v1/v2, p50/p95,
tools, llamadas/tokens, hallucination flags, exactitud de handoff/pedido y stale
context. Los turnos sin rúbrica semántica quedan `REVIEW`, nunca `PASS` por
suposición. Se pueden agregar offline `expected_tools`,
`expected_handoff_reason`, `expected_order_number`, `v1_outcome` y
`rubric_outcome` al export; esos campos no son consumidos por producción.

## Activación futura en Railway

Sólo después de aprobar esta etapa:

1. Aplicar `011_fred_v2_shadow_observations.sql` a la base correcta y validar
   permisos de INSERT/SELECT únicamente sobre observabilidad.
2. Agregar `FRED_V2_SHADOW_TIMEOUT_SECONDS=12`.
3. Agregar `FRED_V2_SHADOW_ENABLED=true`.
4. Desplegar primero a una instancia controlada, verificar líneas
   `[FredShadow]` y confirmar `side_effects=false`.
5. Para rollback, volver `FRED_V2_SHADOW_ENABLED=false`; v1 no cambia.

En esta etapa no se aplicó SQL remoto, no se agregó ninguna variable a Railway,
no se activó el flag y no se desplegó.
