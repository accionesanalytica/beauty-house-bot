# Estado del proyecto Shoow — todo lo que necesitás para arrancar

Este doc es autocontenido: no hace falta que tengas acceso a la carpeta completa del proyecto ni a los otros docs. Pegale esto a tu propia IA/asistente para que tenga contexto completo y te ayude a avanzar en paralelo.

## Qué es el proyecto
Bot de WhatsApp para atención al cliente y gestión de pedidos del ecommerce de Isa. Todo vive en una sola tienda de Tiendanube ("**Beauty House**"), que agrupa dos líneas de producto: maquillaje importado (Beauty House) y pestañas/accesorios (**Shoow Tools**, marca propia de Isa — de ahí el nombre del proyecto). Modelo de negocio: fee de setup + retainer mensual, pensado para después ofrecerlo a otros comercios chicos, no solo a Isa.

Bandera del proyecto: **simplicidad y eficiencia operativa por sobre funcionalidades extra.** No es un ERP tipo La Pyme (facturación, tesorería, contabilidad) — es acotado a inventario + atención por WhatsApp, nada más.

## El plan en 4 fases (+ Fase 0 bloqueante)

**Fase 0 — Conteo físico + reglas de negocio (bloqueante, en curso).** Conteo físico de stock en un Excel nuevo → cruce contra Tiendanube → reglas de negocio definidas. Es la base de todo: hasta que no cierre, no se toca lógica que impacte stock/pedidos reales.

**Fase 1 — Mapeo de FAQs.** A partir de una muestra de chats de WhatsApp exportados a mano (15-25 conversaciones representativas) + PDFs que Isa ya comparte + lo que ya está publicado en su web.

**Fase 2 — Integración API Tiendanube.** Creación de pedidos, consulta de stock.

**Fase 3 — Impresión de etiquetas (RPA).** Posiblemente mucho más chico de lo pensado — ver hallazgo abajo.

**Fase 4 — Testing** de cada módulo antes de integrar todo.

## Reglas de negocio ya cerradas (no re-preguntar a Isa)

**Stock vs. encargo:** Shoow Tools (pestañas) es mayormente stock físico, se encarga si no hay. Maquillaje (Beauty House) es casi siempre a pedido/encargo. El tag de "encargo" no lo define Isa — lo definimos nosotros y ella lo sigue. Propuesta: `encargo`, `encargo-mayorista`, `encargo-minorista` (falta cerrar el nombre final y la regla de default para campo vacío).

**Plazo de espera para encargos:** 7 a 20 días hábiles.

**Envíos reales (IMPORTANTE — corrige una hipótesis inicial errónea):** NO es Mercado Envíos. Del export real de ventas: retiro en persona (varios tags: "Punto de retiro", "Beauty House", "MANDO A RETIRAR", "Retirado en Punto de Venta"), **Envío Nube - Correo Argentino Clásico**, **Envío Nube - Andreani**, y **"Siempre Logística"** (mensajería premium con ventana horaria). Uber se usa solo como excepción manual para casos VIP/urgentes — no es un medio de envío real del sistema, no hay que automatizarlo; cuando aplica, Isa lo resuelve por fuera del flujo.

**Hallazgo clave para Fase 3:** Correo Argentino y Andreani vía "Envío Nube" suelen traer generación de etiqueta integrada nativamente en Tiendanube. Si se confirma, el RPA de etiquetas se reduce muchísimo o directamente no hace falta para esos dos — solo se necesitaría para transportistas sin integración nativa (ej. Siempre Logística). **Esto hay que confirmarlo antes de programar nada de RPA — puede ahorrar toda una fase de trabajo.**

**Cambios y devoluciones (ya publicado en la web de Isa):** procesamiento hasta 5 días hábiles; devoluciones dentro de 7 días corridos (producto sin uso, embalaje intacto); preventa/oferta no elegible a devolución monetaria (sí a cambio, hasta 5 días hábiles); envío de devolución a cargo del cliente salvo falla de fabricación o envío incorrecto; reembolsos 7-10 días hábiles al mismo medio de pago. Para encargos de maquillaje importado: no hay devolución monetaria de encargos ya formalizados, no hay responsabilidad por demoras de Aduana, todo producto se revisa antes de enviarse, no se despacha por moto-mensajería externa.

**El problema de fondo:** hoy Tiendanube NO es fuente de verdad de nada. Isa carga una orden de compra manual pero eso no descuenta ni actualiza stock — es un registro suelto, no un sistema. Esto es lo que el sincronizador tiene que resolver de raíz, más allá de cualquier tag.

**Aprobación manual (MVP):** toda operación pide aprobación de Isa antes de procesar el "cupón de compra" — nada de automatización total desde el día uno. El umbral se va a ir ajustando con el tiempo.

**Interfaz de Isa con el bot:** va a ser **solo WhatsApp** — ella misma pidió poder hablarle a la IA por WhatsApp si tiene dudas, en un hilo separado del de clientes. No hace falta panel web para el MVP.

**Fuera de alcance del MVP:** clientes mayoristas con condiciones especiales (se suma después si hace falta). No se identificaron reglas no escritas de Isa todavía, pero puede aparecer algo en el relevamiento de FAQs.

## Hallazgos del export de datos reales (Tiendanube, agosto 2026)
- Export de productos: 1507 filas (producto + variante). **505 sin control de stock** (campo vacío = posible "sin límite") y **490 con stock=0 pero publicados**. Confirma que el problema es falta de disciplina de carga, no solo falta de un tag.
- No existe ningún tag de "encargo" hoy (se revisaron los 37 tags usados en el catálogo).
- 176 de 1507 filas están asociadas a la marca Shoow Tools.
- Export de ventas: canales registrados son "Web", "Punto de venta", "Móvil" — no está claro si una venta que arranca en WhatsApp/Instagram queda bien registrada o se pierde en "Móvil"/"Punto de venta" manual. Es parte del mismo problema de fondo.

## Lo que podés arrancar YA, sin esperar nada de Luis
1. **Dar de alta la app de Tiendanube** en partners.tiendanube.com y conseguir credenciales de API (OAuth). No depende de nada pendiente.
2. **Confirmar si Envío Nube ya genera etiquetas automáticas** para Correo Argentino y Andreani desde el panel de Tiendanube (revisar documentación de Tiendanube). Esto puede simplificar mucho o eliminar la Fase 3.
3. **Comparar stack de WhatsApp:** WhatsApp Business API oficial de Meta vs. Twilio vs. no oficial (Baileys, whatsapp-web.js). Armar tabla de costo / tiempo de setup / estabilidad / riesgo de baneo (las no oficiales son más rápidas de arrancar pero pueden banear el número). Todavía no elegido.
4. **Investigar y prototipar la lógica general de un bot de WhatsApp simple** (sin meter todavía la lógica de negocio específica de Isa) — esto es justo para que vayas ganando terreno técnico aunque no tengas el contexto completo de negocio. Ideas de qué mirar:
   - Cómo recibir mensajes entrantes (webhook) y responder, con la librería/API que se termine eligiendo en el punto 3.
   - Patrón simple de "intención": clasificar si el mensaje es una FAQ (horarios, envíos, cambios) vs. un pedido nuevo vs. otra cosa — no hace falta IA compleja para el MVP, puede alcanzar con reglas simples + un LLM liviano para las respuestas de FAQ.
   - Cómo mantener el "estado" de una conversación (ej. cliente está a mitad de un pedido) de forma simple — no hace falta una base de datos compleja para el volumen que maneja Isa.
   - Estructura de proyecto básica: separar el manejo de mensajes de WhatsApp, la lógica de FAQs, y la futura integración con Tiendanube en módulos independientes, para que cuando cerremos las reglas de negocio sea fácil enchufar la lógica real sin reescribir todo.
   - Esto es exploración/aprendizaje, no hace falta que sea código final — la idea es que cuando definamos la lógica real de Isa, ya tengas el esqueleto técnico armado y entendido.
5. **Setup de entorno:** Git, editor (VS Code), y el lenguaje que se termine eligiendo según el punto 3 (algunas librerías de WhatsApp solo tienen SDK en Node — puede condicionar la elección de lenguaje).

## Lo que TODAVÍA no hay que tocar
- No escribir lógica que toque stock o pedidos reales de Isa — el conteo físico (Fase 0) no está terminado todavía.
- No arrancar el desarrollo final del bot con la lógica de negocio real hasta tener Meta Business y el trámite de ARCA resueltos del lado de Luis.

## Verificación de Meta Business (en curso, lado de Luis)
Para que sepas por qué puede haber demora: se está verificando el negocio en Meta Business Manager (business.facebook.com), que exige que el nombre legal y domicilio coincidan letra por letra entre la Constancia de ARCA, lo cargado en Meta, y la web. Apareció una discrepancia de dirección (ARCA dice una cosa, la web/Meta otra) que hay que resolver antes de poder completar la verificación y habilitar el número de WhatsApp para el bot oficial.

## Contacto / referencias públicas de Isa (por si sirve de contexto)
- Web: beautyhousemakeup.com
- WhatsApp de atención: +54 9 11 2452-8750
- Domicilio publicado en la web: Vidal 2680, Belgrano, CABA
