# Handoff técnico: Fred v1 → Fred v2 Agent

Estado de referencia: rama `fred-v2-agent`, creada desde `origin/main` en
`2442c9a` (`Merge pull request #10 from accionesanalytica/fred-mvp-reliability`,
2026-08-18). Este documento define la frontera de la migración; no implementa
todavía la reescritura.

## 1. Estado verificado de Fred v1

### Base operativa que se conserva

- Canal: Meta WhatsApp Cloud API y webhook FastAPI en `bot/app.py`.
- Hosting/configuración: Railway, con comportamiento controlado por variables de
  entorno y sin secretos en el repositorio.
- Persistencia: Supabase/Postgres para conversaciones, estado de Fred, pendientes,
  observabilidad y cola durable.
- Cola durable: ingestión idempotente por `wa_message_id`, agrupación de burbujas,
  generation/watermark, leases y worker. Sigue opt-in:
  `DURABLE_MESSAGE_PROCESSING_ENABLED=false` por defecto.
- Knowledge: corpus aprobado versionado, retrieval local o Supabase/pgvector y
  fallback local. `KNOWLEDGE_RAG_SOURCE=local` es el valor seguro por defecto.
- Comercio: Tiendanube es fuente de verdad de producto, variante, SKU, stock,
  precio, pedido, checkout y pago.
- Catálogo: búsqueda léxica/semántica para identificar candidatos; toda afirmación
  comercial cambiante requiere lectura live.
- Seguridad de venta: el modelo no crea pedidos, no modifica stock y no habilita
  una transición sensible por sí solo. Checkout real está desactivado salvo
  `TIENDANUBE_CHECKOUT_MODE=production` y requiere aprobación humana/revalidación.
- Operación humana: Isa recibe o retoma casos y conserva la decisión final en
  excepciones y acciones comerciales sensibles.
- Logs y pruebas: observabilidad por turno sin copiar texto sensible, tests unitarios
  simulados y evaluaciones live separadas/read-only.

### Cómo funciona hoy el cerebro

Fred v1 no es sólo un agente. El turno pasa por una combinación de:

1. estado/máquina de modos y atajos deterministas en `bot/app.py`;
2. clasificación previa de intención y requisito de datos en
   `bot/routing_policy.py`;
3. retrieval de Knowledge y catálogo;
4. agente DeepSeek compatible con function calling en `bot/agent.py`;
5. validación de decisión, argumentos, herramientas y respuesta;
6. filtros posteriores que corrigen, recortan o reemplazan la salida visible.

El agente ya entiende lenguaje y usa herramientas, pero no es el único dueño de
la interpretación. Hay regex y heurísticas para saludos, intención, referencias,
preguntas de pedido, producto, compra, handoff, respuestas cortas y continuidad.
La acumulación está concentrada especialmente en `bot/app.py` (más de 7.000 líneas)
y `bot/routing_policy.py` (más de 800 líneas).

### Estado funcional y discrepancias conocidas

- En `main`, `RECOMMENDATIONS_ENABLED=False`: Fred informa, pero la recomendación
  personalizada se deriva a Isa.
- La ruta reciente trata intención de compra como handoff a Isa y evita abrir
  checkout desde el chat. Esto no coincide por completo con documentos anteriores
  que describen una ficha de compra activa; el código vigente manda.
- `BOT_RESPONSE_MODE` conserva `template` como default, mientras el modo de agente
  se activa por configuración.
- `SALES_INTAKE_ENABLED=false`, cola durable desactivada y checkout real desactivado
  son defaults seguros. La configuración efectiva de Railway no fue consultada ni
  modificada en esta preparación.
- `main` no incluye todavía `c308fbf` (`Answer a social message as a social message,
  whatever came before`), que existe en `fred-mvp-reliability`. No se lo incorporó
  silenciosamente: debe evaluarse o migrarse como caso de aceptación de v2.

### Línea base de tests

Ejecutado localmente sobre `2442c9a`:

```text
python -m unittest discover -s tests -v
Ran 661 tests in 1.198s
OK
```

La suite usa dobles y bloquea envíos reales. No prueba el modelo real, Meta,
Railway, Supabase ni Tiendanube live. Python 3.9 emitió advertencias de fin de vida
y LibreSSL; no causaron fallas, pero conviene tratarlas fuera de esta migración.

## 2. Contrato objetivo de Fred v2

### Principio

Un único LLM es dueño de comprender el mensaje actual y el contexto conversacional,
incluidos saludos deformados, agradecimientos, cambios de tema, elipsis y referencias
breves. No se agregan listas de frases ni clasificadores regex para ampliar la
comprensión semántica.

El determinismo se conserva donde protege hechos, permisos o efectos: autenticación,
validación de argumentos, identidad de producto/pedido, datos live, idempotencia,
estado durable, límites de herramientas, aprobación humana, checkout y escritura en
sistemas externos.

### Superficie cerrada de herramientas del cerebro v2

El LLM sólo ve estas cuatro herramientas de dominio:

#### `search_knowledge`

- Entrada: consulta natural y, si corresponde, contexto de tema.
- Lee únicamente Knowledge aprobado (local/Supabase con el fallback vigente).
- Devuelve hechos, vigencia, obligaciones, enlaces aprobados y requisitos dinámicos.
- No devuelve stock, precio, tracking ni otros datos live como si fueran estáticos.

#### `get_order`

- Entrada: número de pedido validado.
- Encapsula la consulta read-only a Tiendanube y devuelve un resultado tipado.
- Si falta el número, el LLM lo pide naturalmente; no se adivina desde números
  ambiguos ni se consulta catálogo.

#### `get_product`

- Entrada: referencia natural, nombre/link/SKU y la necesidad solicitada
  (identidad, variantes, stock o precio).
- Encapsula búsqueda de catálogo, resolución de candidatos y revalidación live.
- Devuelve evidencia tipada con identidad, publicación, variante, SKU, stock,
  precio y ambigüedad. El wrapper, no el LLM, decide qué afirmaciones están
  respaldadas por Tiendanube.

#### `handoff_to_isa`

- Entrada: motivo enumerado y resumen breve estructurado.
- Entrega el canal/flujo humano existente sin exponer texto interno a la clienta.
- No autoriza checkout ni crea una orden. Debe ser idempotente y auditable.

### Invariantes no negociables

- Una sola llamada/orquestación de agente puede incluir varias tool calls, pero no
  hay otro clasificador semántico paralelo que contradiga su intención.
- El mensaje actual manda; el historial sólo aporta contexto.
- Ningún precio, stock, estado de pedido o link comercial se inventa o sale de
  memoria del modelo.
- Una tool no disponible/fallida produce aclaración segura o handoff, nunca un dato
  simulado.
- Código cerrado valida esquema, permisos, frescura, límites y transición antes de
  ejecutar efectos.
- WhatsApp, FastAPI, Railway, Supabase/Postgres, cola durable, Knowledge, Tiendanube,
  catálogo, logs y tests se reutilizan: v2 reemplaza el cerebro, no la plataforma.
- Regex queda permitido para sintaxis cerrada (teléfono, email, identificadores,
  firmas, comandos internos de Isa, normalización), no como motor principal de
  intención o lenguaje cotidiano.
- No se despliega, activa un flag productivo, migra Supabase ni escribe en Tiendanube
  durante esta fase.

## 3. Plan de migración incremental

### Fase 0 — Congelar contrato y corpus

1. Convertir conversaciones problemáticas y casos ya cubiertos por v1 en un corpus
   versionado con entrada, contexto, tools esperadas/prohibidas y criterios visibles.
2. Etiquetar por tipo: social, Knowledge, pedido, producto, cambio de tema, elipsis,
   ambigüedad, compra/handoff y falla de proveedor.
3. Registrar baseline de v1 sin llamadas ni escrituras productivas.

Salida: corpus revisado por humanos y métricas reproducibles.

### Fase 1 — Adaptadores sin cambio de comportamiento

1. Crear wrappers tipados para las cuatro tools, reutilizando internamente los
   conectores actuales.
2. Mantener guardrails, timeouts, límites, evidencia y observabilidad fuera del LLM.
3. Probar cada wrapper con dobles y contract tests; no conectar todavía el webhook.

Salida: superficie cerrada probada, sin duplicar lógica comercial.

### Fase 2 — Cerebro v2 en shadow

1. Implementar el agente único detrás de `FRED_AGENT_VERSION=v2` o flag equivalente.
2. Ejecutarlo en shadow sobre mensajes sanitizados: genera decisión y tool plan, pero
   no envía WhatsApp, no crea handoffs reales y no escribe en Tiendanube.
3. Comparar con v1 y revisión humana. Corregir primero prompt, contexto o contrato de
   tools; no responder a cada error agregando regex.

Salida: umbrales de calidad y seguridad alcanzados offline/shadow.

### Fase 3 — Canary reversible

1. Activar v2 sólo para teléfonos allowlist internos.
2. Mantener v1 como rollback inmediato y asignación sticky por conversación.
3. Observar diariamente transcriptos permitidos por el proceso, tool errors,
   handoffs y cualquier intento de dato no respaldado.

Salida: aprobación explícita de Isa antes de ampliar tráfico.

### Fase 4 — Migrar tráfico y retirar heurísticas

1. Aumentar 5% → 25% → 50% → 100% sólo si cada ventana cumple los gates.
2. Retirar rutas heurísticas por dominio después de demostrar paridad, no antes.
3. Conservar las validaciones deterministas de seguridad y los tests de regresión.

Salida: v2 dueño de la semántica; v1 disponible temporalmente para rollback.

## 4. Plan A/B y gates

### Asignación

- Unidad: conversación, nunca mensaje individual.
- Sticky assignment: una conversación permanece en v1 o v2.
- Misma plataforma, mismos datos aprobados y mismas restricciones de tools.
- Primero replay offline, luego shadow, luego canary allowlist. No usar clientas reales
  como primer experimento.

### Métricas primarias

- Resolución correcta del turno según revisión humana.
- Exactitud de selección de tool y argumentos.
- Tasa de hechos comerciales respaldados por evidencia live.
- Handoffs correctos, innecesarios y omitidos.
- Repetición/re-pregunta de información ya dada.
- Contaminación por historial o respuesta al tema anterior.
- Latencia p50/p95, llamadas al modelo/tools y costo por turno.

### Gates mínimos sugeridos

- 0 creación de orden, modificación de stock o envío real en replay/shadow.
- 0 afirmaciones de stock/precio/pedido sin evidencia de tool.
- 100% de acciones sensibles bloqueadas por código y aprobación correspondiente.
- No regresión en los 661 tests de v1 mientras comparten plataforma.
- Mejora estadísticamente visible en lenguaje social, elipsis y cambios de tema,
  sin empeorar pedido/producto/Knowledge/handoff.
- Rollback probado antes del canary y aprobación humana antes de cada ampliación.

## 5. Primer corte de implementación propuesto

El primer pull request de código debe ser chico: interfaces y adaptadores de las
cuatro tools, sus contract tests y el flag de versión apagado. No debe reemplazar
`bot/app.py`, tocar Railway/Supabase/Meta, activar la cola durable ni cambiar el
flujo productivo. El segundo corte agrega el agente v2 sólo en replay/shadow.
