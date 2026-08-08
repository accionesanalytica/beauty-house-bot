# Decisiones técnicas

Registro de decisiones tomadas (y pendientes) durante el proyecto, para no perder contexto.

## Estado: 2026-08-03 — kickoff del proyecto

### Conteo físico de stock
- **Estado: CONTEO HECHO Y CRUZADO (2026-08-05).** 344 filas cargadas por Isa en "Conteo Físico Inventario Shoow Tools.xlsx", cubriendo toda la tienda (no solo Shoow Tools). Cruzado contra el export de productos de Tiendanube.
- Resultado del cruce guardado en `/assets/cruce-inventario-fase0.xlsx` (4 hojas: Resumen, Diferencias de cantidad, Sin match en Tiendanube, TN con stock sin conteo físico).
- **Hallazgos clave:**
  - 2911 unidades contadas físicamente en 334 combinaciones producto+variante.
  - 316 combinaciones encontraron match razonable en Tiendanube; 18 no tienen match claro (nombres muy distintos, revisar manual).
  - 131 productos tienen diferencia entre cantidad física y stock de Tiendanube.
  - **160 productos de Tiendanube con stock cargado (sumando 9160 unidades) nunca aparecen en el conteo físico.** Varios con números muy altos y sospechosos en variantes "(1 PAIR)" — ej. "SHOOW TOOLS - ICONIC VOLUME (1 PAIR)" con 1585 unidades. Fuerte candidato a stock fantasma / mal cargado, no a producto real.
  - Del lado físico, 294 de 344 filas no tenían la columna "Estado" completada — se asumió "disponible" salvo que dijera "Mal estado" explícitamente. Falta confirmar con Isa si esa asunción es correcta.
  - Casi ninguna fila del conteo físico tiene SKU ("No sabemos" en casi todas) — el emparejamiento se hizo por similitud de nombre (algoritmo automático), no es 100% confiable, sirve de guía para revisión manual.
- **CORRECCIÓN (2026-08-07) — la primera pasada del cruce tenía errores, detectados por Isa.** Resultado corregido en `/assets/cruce-inventario-fase0-v2.xlsx`. Errores que había:
  1. Se descartaban 600 filas del export de Tiendanube: las variantes vienen con el campo "Nombre" vacío y el script las salteaba. 385 de esas filas tenían stock. Corregido arrastrando el nombre del producto padre y sumando variantes.
  2. El producto genérico "SHOOW TOOLS - Pestañas (10PAIRS)" (stock 0) capturaba todos los matches de "10 pares X" por una falla del algoritmo de similitud. Corregido exigiendo coincidencia en palabras distintivas.
  3. Se afirmó que "casi ningún producto tiene SKU": falso, 500 de 825 productos SÍ tienen SKU.
  4. Stock subreportado por no sumar variantes (ej. TAYLOR BLACK: informado 49, real 91).
- **Números corregidos:** 825 productos únicos en TN, 11.498 unidades de stock total. 172 matches de alta confianza, 124 a revisar, 38 sin match. 114 con diferencia de cantidad.
- **HALLAZGO PRINCIPAL (nuevo):** el problema mayor no es stock faltante sino **productos duplicados en Tiendanube**. 18 grupos / 52 fichas del mismo producto físico cargado con nombres distintos. Ej: "ICONIC VOLUME" existe como 4 fichas — (1 PAIR)=1585, (10PARES)=83, (10PAIRS)=0, sin sufijo=0. Las fichas "(1 PAIR)" concentran números enormes (1585, 1581, 1451, 1427) que no coinciden con nada del conteo físico.
- **Pregunta abierta para Isa:** ¿las fichas "(1 PAIR)" venden pestañas sueltas por unidad o son fichas viejas mal cargadas? El conteo físico cuenta CAJAS de 10 pares — si "(1 PAIR)" vende unidades sueltas de esas mismas cajas, el stock debe estar vinculado, no duplicado.
- **TERCERA PASADA (2026-08-07) tras auditoría externa.** Una segunda IA auditó el cruce y detectó falsos positivos por encima del umbral de confianza y falsos negativos por debajo. Se verificaron sus 4 ejemplos concretos: **los 4 eran reales**. Resultado en `/assets/cruce-inventario-fase0-v3.xlsx`.
  - **Causa raíz:** el algoritmo no usaba la columna "Marca" del conteo físico. La columna "Marca" de Tiendanube está vacía (24 de 1507 filas) pero la marca está dentro del nombre del producto; ahora se extrae de ahí.
  - **Reglas duras nuevas:** marca incompatible → se descarta el match; sin marca pero categoría incompatible → se descarta; coincidencia de marca → bonus de score.
  - **Corregidos:** "corrector natural finished" (aoa studio) iba a pestañas Shoow Tools → ahora a AOA STUDIO. "the blush" (essence) iba a "PESTAÑAS 10 PAR" → ahora a ESSENCE. "Niacinamide serum" pasó de 36.3 sin match a 77.0 con "Good molecules - Niacinamida Serum". "coloricon multistick" de 35.1 a 87.7 con WET N WILD.
  - **Nuevos números:** 255 alta confianza (antes 172), 55 a revisar (antes 124), 24 sin match (antes 38).
  - **Sigue sin ser automático.** Ej: "the blush" ahora acierta marca y categoría pero eligió MOSAIC BLUSH existiendo una ficha "THE BLUSH - ESSENCE". El archivo es mapa de trabajo, no lista de correcciones a aplicar a ciegas.

### Plan de limpieza acordado (recomendación de la auditoría externa, adoptada)
No limpiar los 825 productos antes de avanzar. Orden:
1. **Resolver la pregunta "(1 PAIR)"** con Isa (5-10 min). Pregunta exacta: ¿vende pares individuales de esos modelos? Si sí, ¿tienen stock físico separado o se abren las cajas de 10? Si se abren las cajas, hay riesgo de vender dos veces la misma mercadería → elegir UNA sola unidad de inventario por mercadería física (no construir lógica de stock vinculado en el MVP).
2. **Unificar duplicados de Shoow Tools** (1-2 h). Marca propia, catálogo chico, mayor daño potencial.
3. **Corregir stock de Shoow Tools** contra el conteo físico (1-2 h).
4. **Revisar solo los matches dudosos de productos activos** (1-3 h), no los 55 por obligación.
5. **Congelar el resto**: productos viejos sin existencia física → stock 0 y ocultos.
6. **Recién ahí conectar el MVP** al catálogo.

### SKUs — decisión revisada
Se descarta renombrar masivamente los 500 SKUs existentes (trabajo y riesgo sin beneficio para el bot). Regla adoptada:
- SKU existente y único → se conserva.
- Producto sin SKU → se crea uno nuevo.
- SKU duplicado o incorrecto → se corrige.
- EAN/código de barras → se conserva en su campo, no reemplaza al SKU interno.
- Formato para SKUs nuevos: legible en vez de numérico (`SHW-LASH-FOXY1-BLK` en lugar de `SHW-PES-001-BLK`), para que Isa reconozca el producto mirando el código y sea más difícil duplicar por accidente.

### Matiz sobre "fuente de verdad" (de la auditoría)
Separar dos conceptos durante la limpieza: **fuente de verdad física temporal** = el conteo del depósito; **fuente de verdad digital futura** = Tiendanube una vez reconciliado. Hasta terminar la limpieza el bot no debe prometer disponibilidad automáticamente desde Tiendanube — la aprobación manual del MVP protege contra eso.

### Reglas de negocio en Tiendanube (tags, estados)
- **Estado:** primer borrador consolidado en `/docs/reglas-tiendanube.md` a partir de respuestas de Luis + análisis del export de productos/ventas + políticas publicadas en beautyhousemakeup.com. Todavía falta la validación final de Isa (8 puntos abiertos, ver resumen al final de ese doc).
- Hallazgo clave del CSV: 505 de 1507 filas de producto/variante tienen el campo Stock vacío (posible "sin control de stock") y 490 tienen stock=0 pero siguen publicados — confirma que el problema de fondo es falta de disciplina de stock, no solo falta de un tag de "encargo".
- No existe hoy ningún tag de "encargo" en el catálogo (se revisaron los 37 tags usados).
- **Bloqueante para Fase 1 y 2:** hasta que Isa confirme los puntos abiertos, no arranca el desarrollo del bot ni de la integración API.

### Stack de WhatsApp
- **Estado: DECIDIDO (2026-08-04) — WhatsApp Cloud API oficial de Meta.**
- **Verificación de Meta Business: COMPLETADA (2026-08-07).** Se destrabó agregando la razón social (MARSHALL PEREZ ISABELLA MARIA) al pie de página global de beautyhousemakeup.com, además de las páginas de Políticas y Contacto.

### Número de teléfono del bot
- **Estado: DECIDIDO (2026-08-07) — se arranca con el número de prueba gratuito de Meta.**
- Motivo: un número solo puede estar en WhatsApp Business App **o** en Cloud API, nunca en las dos. El número actual de Isa (+54 9 11 2452-8750) está en la app; migrarlo implicaría que ella pierde el acceso desde el celular.
- Con el número de prueba se desarrolla y prueba todo sin tocar la operación actual.
- **DECISIÓN DIFERIDA (pendiente, importante):** qué número usa el bot en producción. Tres caminos: (a) seguir con número nuevo publicado en web/IG — Isa mantiene su WhatsApp intacto pero los clientes viejos siguen escribiendo al número de siempre; (b) migrar el número principal — el bot atiende donde los clientes ya escriben, pero **hay que resolver cómo Isa ve y responde las conversaciones**, porque pierde la app del celular. Esto último no estaba contemplado en el plan original y hay que diseñarlo antes de migrar.

### Lenguaje / stack de desarrollo
- **Estado: DECIDIDO (2026-08-04) — Node.js + Express.** Elegido por ser la combinación más madura/documentada para integrar con WhatsApp Cloud API oficial, y por desplegar sin fricción en Railway.

### Hosting
- **Estado: DECIDIDO (2026-08-04) — Railway.** Se sube el repo y despliega solo, con HTTPS público (necesario para el webhook de Meta) sin mantener servidor propio. Coincide con la bandera de simplicidad — cero administración de infraestructura.

### Base de datos
- **Estado: DECIDIDO (2026-08-04) — Postgres (addon de Railway).**
- **Corrección importante (2026-08-04, catch del hermano de Luis):** Postgres NO guarda pedidos como registro definitivo — eso recrearía el problema de fondo (Tiendanube dejando de ser fuente de verdad). Tiendanube es SIEMPRE la única fuente de verdad de pedidos y stock reales.
- Lo que Postgres sí guarda: (1) estado de la conversación de WhatsApp (en qué paso está cada cliente), (2) cola de pedidos en BORRADOR pendientes de aprobación de Isa — recién cuando ella aprueba, el bot llama a la API de Tiendanube y ahí nace el pedido real; el borrador se descarta o marca como procesado, no queda como duplicado, (3) log de mensajes para que Isa pueda repasar conversaciones.

### Base de conocimiento de FAQs
- **Estado: DECIDIDO — sin motor de búsqueda ni base vectorial.** El documento de FAQs se carga directo como contexto del bot. Alcanza para el volumen de Isa, coincide con simplicidad.

### RPA de etiquetas — transportista
- **Estado:** hipótesis de Mercado Envíos DESCARTADA — no aparece en el export real de ventas.
- El export de ventas (agosto 2026) muestra: retiro en persona (varias variantes de tag), Envío Nube - Correo Argentino Clásico, Envío Nube - Andreani, y "Siempre Logística" (mensajería premium con ventana horaria). Uber fue mencionado por Luis pero no aparece en la muestra de datos — a confirmar con Isa.
- **Hallazgo clave:** Correo Argentino y Andreani vía "Envío Nube" suelen traer generación de etiqueta integrada nativamente en Tiendanube — podría no hacer falta RPA para esos dos. El RPA (si hace falta) se acotaría a los transportistas sin integración nativa, ej. Siempre Logística.
- **Próximo paso:** revisar en el panel de Tiendanube si Envío Nube ya genera etiquetas automáticas para Correo Argentino/Andreani, y confirmar con Isa el estado real de Siempre Logística y si Uber sigue en uso.

### API Tiendanube
- **Estado:** todavía no hay cuenta de desarrollador ni API key.
- **Próximo paso:** dar de alta la app en el panel de desarrolladores de Tiendanube (parte del arranque de fase 2).

### Datos existentes (Excel finanzas, CSV ventas)
- **Estado:** existen pero no están cargados al repo todavía.
- No se suben archivos originales de Isa a `/assets` sin su autorización explícita — evaluar si van completos o solo un resumen/schema.

### Fuente de FAQs
- **Estado:** a relevar desde cero.
- Fuentes propuestas: export de chats de WhatsApp de Isa con clientes + PDFs que Isa ya comparte con sus clientes (políticas de envío, cambios, talles, etc.).
- **Próximo paso:** conseguir ambos insumos para arrancar el análisis en fase 1.

### Modelo de monetización (pricing)
- **Estado:** definido a nivel concepto (fee de setup + retainer mensual), sin montos ni estructura de planes documentada todavía.
- **Próximo paso:** si se quiere documentar en el repo, armar `/docs/pricing.md` con montos y qué incluye cada plan. Por ahora queda fuera del repo (es info de negocio, no técnica) salvo que se pida lo contrario.
