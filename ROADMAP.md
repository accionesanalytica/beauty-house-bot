# Fred — Roadmap

## Estado actual

Fred está desplegado en Railway y ya integra WhatsApp, Tiendanube y
Supabase/Postgres. Atiende consultas, consulta catálogo y disponibilidad,
arma una ficha de venta y pide aprobación humana antes de crear un checkout.

La arquitectura de calidad ya está incorporada: contexto acotado, búsqueda de
catálogo, conocimiento revisado, decisiones estructuradas, reglas de
seguridad, pruebas y observabilidad.

## DONE

- Atención por WhatsApp y registro de conversaciones.
- Consulta de catálogo, precio y stock desde Tiendanube.
- Ficha de compra con aprobación de Isa antes del checkout.
- Flujos separados para encargo, preventa y venta mayorista.
- Context engineering, RAG de catálogo y Knowledge RAG revisado.
- Guardrails, decisiones estructuradas y pruebas locales sin consumo de IA.
- Observabilidad y panel operativo MVP.
- Kit replicable para instalar Fred en otro comercio sin reutilizar secretos.

## VALIDATING

- Knowledge RAG: ya indexado y activado; falta validar respuestas reales de
  políticas, cambios, encargos y preventa.
- Calidad conversacional: revisar conversaciones reales, en especial
  recomendaciones, correcciones, escalaciones y compras.
- Checkout real: hacer una prueba controlada de punta a punta y confirmar la
  configuración productiva antes de abrir ventas al público.

## BLOCKED

- Número propio de Fred en Meta: pendiente de recibir e instalar el eSIM de
  Movistar, verificarlo y registrarlo en WhatsApp Cloud API.
- Observación de conversaciones desde WhatsApp Business: depende de validar
  coexistencia con Meta para el nuevo número.
- Avisos posteriores al pago y resumen diario: requieren que las plantillas de
  Meta correspondientes estén aprobadas y configuradas.
- Cualquier automatización que modifique stock: requiere reglas de inventario
  y reconciliación confirmadas con Isa.

## NEXT

1. Probar Knowledge RAG con preguntas reales y revisar exactitud/tono.
2. Hacer un checkout controlado de bajo valor y verificar pedido, pago y stock
   en Tiendanube.
3. Registrar el nuevo número de Fred en Meta cuando llegue el QR del eSIM.
4. Habilitar el número al público de forma gradual y revisar las primeras
   conversaciones todos los días.

## Más adelante

- Mejorar el panel de Isa para supervisar conversaciones y pendientes.
- Medir resolución, escalaciones, conversiones y costo por conversación.
- Convertir el kit replicable en un proceso de alta para otros comercios.
- Evaluar un portal multi-cliente solo cuando haya varios comercios operando
  con necesidades reales en común.

## Lectura recomendada

1. `docs/operacion-fred.md` — operación diaria y límites actuales.
2. `docs/arquitectura.md` — visión general del sistema.
3. `docs/kit-replicable-fred.md` — cómo llevar el modelo a otro comercio.
4. `docs/checkout-real.md` — flujo y controles de una venta normal.
5. `docs/qa-fred-mvp.md` — qué se prueba antes de abrir al público.

`README.md` y `docs/estado-para-hermano.md` se conservan como historia del
arranque; no son la guía operativa actual. Ver `docs/documentacion-vigente.md`.
