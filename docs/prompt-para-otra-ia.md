# Prompt para pasar a otra IA (revisión cruzada del cruce de inventario)

Copiá todo lo que está debajo de la línea y pegalo en el otro chat, junto con los archivos: `Conteo Físico Inventario Shoow Tools.xlsx`, el export de productos de Tiendanube (`tiendanube-...csv`) y `cruce-inventario-fase0-v2.xlsx`.

---

## Contexto

Estamos automatizando la atención al cliente y la gestión de stock de un ecommerce argentino de cosmética. La tienda se llama **Beauty House** (Tiendanube) y agrupa dos líneas: maquillaje importado y **Shoow Tools**, la marca propia de pestañas y accesorios de la dueña (Isa). El objetivo final es un bot de WhatsApp que responda consultas y arme pedidos, con la API de Tiendanube por detrás.

**Bandera del proyecto: simplicidad y eficiencia operativa por encima de todo.** No queremos un ERP. No queremos features de más.

Antes de programar nada decidimos ordenar la base, porque detectamos que **Tiendanube hoy no es fuente de verdad**: Isa carga órdenes manuales que no descuentan stock, vende también por WhatsApp e Instagram sin que eso impacte el sistema, y el catálogo está sucio. Automatizar sobre eso sería multiplicar el problema.

## Lo que se hizo hasta ahora

1. Isa hizo un **conteo físico completo del depósito**: 344 filas, 2911 unidades, cubriendo toda la tienda (no solo Shoow Tools). Es una planilla informal — nombres como "Cluster Elizabeth Taylor" o "10 pares foxy #1", casi sin SKU (dice "No sabemos" en la mayoría), y la columna "Estado" quedó vacía en 294 de las 344 filas.
2. Se cruzó ese conteo contra el export de productos de Tiendanube (1507 filas = 825 productos únicos con sus variantes) usando emparejamiento automático por similitud de nombres.

**El primer intento del cruce tuvo errores que Isa detectó. Se corrigieron y se rehizo. Los errores fueron:**

- Se descartaban 600 filas del export: en Tiendanube las variantes vienen en filas con el campo "Nombre" vacío, y el script las salteaba. 385 de esas filas tenían stock cargado.
- Un producto genérico llamado "SHOOW TOOLS - Pestañas (10PAIRS)" (stock 0) capturaba todos los matches de "10 pares X" por una falla del algoritmo de similitud, en vez de matchear con el producto real.
- Se afirmó erróneamente que casi ningún producto tenía SKU. En realidad **500 de 825 productos sí tienen SKU cargado**.
- Stock subreportado por no sumar variantes (ej: TAYLOR BLACK se informó con 49 unidades, el real es 91 repartido en 2 variantes).

## Resultados de la segunda pasada (corregida)

- 825 productos únicos en Tiendanube, 11.498 unidades de stock total sumando variantes.
- 172 emparejamientos de alta confianza, 124 de confianza media (a revisar), 38 sin match razonable.
- 114 productos con diferencia entre la cantidad física real y lo que dice Tiendanube.

**Hallazgo principal: el problema más grande no es stock faltante, son productos duplicados en Tiendanube.** Hay 18 grupos (52 fichas) del mismo producto físico cargado varias veces con nombres distintos. Ejemplos reales:

- "ICONIC VOLUME" → 4 fichas: `(1 PAIR)` con 1585, `(10PARES)` con 83, `(10PAIRS)` con 0, y una sin sufijo con 0.
- "SOFTLY BEAUTIFUL" → 3 fichas: `(1 PAIR)` con 1581, `(10PAIRS)` con 47, sin sufijo con 0.
- "FOXY #1" → 6 fichas, casi todas en 0.
- Además hay inconsistencias de nomenclatura: "SHOOW TOOLS" vs "SHOOWTOOLS", "(10PAIRS)" vs "(10 PAIRS)" vs "(10PARES)".

Las fichas `(1 PAIR)` concentran números enormes (1585, 1581, 1451, 1427) que no coinciden con nada del conteo físico. El conteo físico cuenta **cajas de 10 pares**, no unidades sueltas.

## Regla que ya definimos y no está en discusión

**El conteo físico es la fuente de la verdad.** Lo que está en el depósito es lo real; Tiendanube tiene que adaptarse a eso, no al revés. Cualquier corrección va en la dirección de hacer que Tiendanube refleje el conteo, nunca de "confiar" en un número del sistema que no se pudo verificar físicamente.

Otras decisiones ya tomadas (no hace falta re-discutirlas salvo que veas un problema grave):
- Stack: WhatsApp Cloud API oficial de Meta + Node/Express + Railway + Postgres + Claude API (Haiku) para el lenguaje natural.
- Postgres NO guarda pedidos como registro definitivo — Tiendanube es siempre la fuente de verdad de pedidos y stock. Postgres solo guarda estado de conversación, borradores pendientes de aprobación y log de mensajes.
- En el MVP, toda operación pide aprobación manual de Isa antes de procesarse.
- Las correcciones de catálogo se hacen a mano en el panel de Tiendanube, NO por import masivo de CSV (demasiado riesgo de pisar datos con la base así de sucia).

## Lo que necesito de vos

### 1. Verificación crítica (triple chequeo si hace falta)
Revisá los tres archivos por tu cuenta y **buscá activamente errores en el análisis anterior**. Ya se cometieron errores importantes una vez, así que asumí que puede haber más. Puntos concretos a verificar:
- ¿El emparejamiento por similitud de nombres tiene más falsos positivos o falsos negativos? Especialmente en los 124 de confianza media.
- ¿Hay más grupos de duplicados que no se detectaron?
- ¿La suma de stock por variantes está bien hecha?
- ¿Qué hacemos con las 294 filas del conteo físico sin "Estado" completado? El análisis anterior asumió que estaban disponibles salvo que dijeran "Mal estado" explícitamente. ¿Es una asunción razonable o hay que tratarlas distinto?
- ¿Hay algún supuesto del análisis anterior que no se sostiene?

Si encontrás errores, decilo directamente y mostrá la evidencia.

### 2. La pregunta abierta que hay que resolver
¿Qué son las fichas `(1 PAIR)` con stock enorme? Dos hipótesis:
- **(a)** Venden pestañas sueltas por unidad, sacadas de las mismas cajas que se contaron físicamente. Si es así, hay riesgo de vender dos veces la misma mercadería y el stock tiene que estar vinculado de alguna forma.
- **(b)** Son fichas viejas o mal cargadas que quedaron con números inventados y hay que darlas de baja o ponerlas en cero.

Decime qué evidencia hay en los datos a favor de cada una y cuál te parece más probable.

### 3. Tu opinión sobre los SKUs
Hoy 500 de 825 productos tienen SKU y el resto no. En el conteo físico casi ninguna fila tiene SKU. Se propuso un esquema tipo `[MARCA]-[CATEGORÍA]-[NÚMERO]-[VARIANTE]`, por ejemplo `SHW-PES-001-BLK`.

**¿Te parece la mejor opción o proponés otra cosa?** Considerá que Isa va a cargar esto a mano, que ya hay 500 SKUs existentes que quizás convenga respetar en vez de reemplazar, y que algunos productos tienen código de barras real (EAN) cargado. Decí qué harías vos y por qué.

### 4. Tu propuesta de plan
Dado todo lo anterior, **¿cuál es tu plan recomendado?** No repitas simplemente lo que ya propusimos — si pensás que el orden debería ser otro, o que hay un atajo, o que algo de lo planteado es innecesario o riesgoso, decilo. Priorizá siempre simplicidad operativa: Isa tiene un negocio que atender, no puede dedicarle semanas a esto.

### 5. Lo más importante: explicárselo a Isa
Isa **no es técnica**. Escribile un mensaje directo a ella, en español rioplatense, simple y sin jerga, que le explique:
- Qué encontramos (en criollo, sin números que abrumen).
- Por qué importa para su negocio en concreto (qué le puede pasar si no lo arregla).
- Qué tiene que hacer ella exactamente.

Y cerrá **ofreciéndole opciones concretas para elegir, en formato de selección simple**, algo como:

> **¿Cómo querés encarar la limpieza del catálogo?**
> - **A)** [opción, con cuánto tiempo le lleva y qué gana]
> - **B)** [opción, con cuánto tiempo le lleva y qué gana]
> - **C)** [opción, con cuánto tiempo le lleva y qué gana]

Las opciones tienen que ser realistas, con el esfuerzo estimado, y que ella pueda responder con una sola letra. Ejemplos del tipo de eje que podrían tener: arreglar todo de una vs. arreglar solo lo que más se vende y el resto después, hacerlo ella sola vs. con ayuda, empezar por los duplicados vs. empezar por las cantidades. Elegí vos los ejes que te parezcan más útiles según lo que veas en los datos.
