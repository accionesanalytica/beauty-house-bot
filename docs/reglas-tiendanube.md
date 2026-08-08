# Reglas de negocio — Tiendanube / Beauty House / Shoow

Consolidado a partir de: respuestas de Luis (2026-08-03), export de productos de Tiendanube (`tiendanube-...csv`, 1507 filas de producto+variante), export de ventas (`ventas-...csv`, 92 filas), y contenido publicado en beautyhousemakeup.com (`/politicas/`, `/pedidos-especiales/`).

**Importante:** todo lo que dice "confirmar con Isa" es un hallazgo mío o de Luis, no una regla ya validada por Isa. No usar como definitivo hasta que ella lo confirme.

## Contexto de tiendas
- Todo vive en una sola tienda de Tiendanube: **Beauty House**.
- Dos líneas de producto conviven ahí: maquillaje/importados (Beauty House propiamente) y **Shoow Tools** (pestañas y accesorios, marca propia de Isa). En el export, 176 de 1507 filas están asociadas a Shoow.
- No hay tienda separada para Shoow — el nombre del proyecto ("Shoow") es la marca, no una tienda distinta en Tiendanube.

## 1. Stock real vs. hecho por encargo
- **Shoow Tools** (pestañas, marca propia): mayormente stock físico. Si no hay stock, se encarga — depende del cliente/pedido puntual.
- **Maquillaje** (Beauty House, productos importados): casi siempre a pedido/encargo.
- Puede haber mayoristas de esponjas/pestañas que cambien esta regla — caso a caso.
- Isa opera hoy con un punto de venta (POS) donde carga productos a mano; el problema real es que **algunos productos tienen cantidad "infinita" (sin control de stock) y otros no** — es un mix sin criterio consistente hoy.
- **Confirmado en el CSV:** de 1507 filas, 505 tienen el campo Stock vacío (posible equivalente a "sin límite / no controla stock") y 490 tienen stock = 0 pero siguen publicados. Esto es el problema de raíz que hay que resolver en la Fase 0 antes de definir el tag de "encargo".
- **Tag de "encargo":** no existe hoy (se revisaron los 37 tags usados en el catálogo, ninguno es de este tipo). A definir: nombre exacto y qué pasa si un producto no tiene tag (¿default a "stock real" o "encargo"?) o tiene un tag fuera de la regla.
- **Tiempo de espera:** para "pedidos especiales" (encargos de maquillaje importado), la web publica **15 a 20 días hábiles** desde el pago, dependiendo de origen y cantidad — ver sección 5. Para Shoow Tools hecho a pedido, no hay plazo publicado — **confirmar con Isa**.

## 2. Variantes (talles / colores)
- El control de stock por variante es débil/inconsistente — confirmado por el CSV (muchos vacíos) y por lo que dice Luis.
- No hay proceso de actualización de stock hoy (ni manual sistemático ni automático) — es justamente el problema a resolver.
- Talles/colores con poco stock: no identificado — se resuelve con el conteo físico de Fase 0.

## 3. Estados de pedido
Del export de ventas (muestra de 92 filas / ~40 pedidos únicos, agosto 2026):
- **Estado de la orden:** solo se vio "Abierta" en la muestra (puede haber otros no capturados en esta ventana de tiempo — Cerrada, Cancelada, etc.)
- **Estado del pago:** "Recibido" y "Pendiente"
- **Estado del envío:** "Enviado" y "No está empaquetado"
- No hay tag/estado específico para "esperando que se haga el producto" — no existe distinción entre pedido pagado listo para preparar vs. pedido esperando fabricación/importación.
- Nadie cambia el estado de forma sistemática — a veces Isa lo hace manual. Este es un punto flojo del proceso actual.

## 4. Envíos y etiquetas
**Corrección importante a la hipótesis original:** NO es solo Mercado Envíos. El export de ventas muestra estos medios de envío reales:
- Retiro en persona: "Punto de retiro", "Beauty House", "MANDO A RETIRAR", "Retirado en Punto de Venta"
- Envío Nube - Correo Argentino Clásico a domicilio
- Envío Nube - Andreani a domicilio
- "@Siempre.Logística Servicio Premium" (mensajería con ventana horaria)
- Luis mencionó también Uber como modalidad — no apareció en esta muestra de datos, puede ser una opción marginal o reciente. **Confirmar con Isa.**

Esto cambia el enfoque técnico de Fase 3: **Correo Argentino y Andreani via "Envío Nube" suelen tener generación de etiqueta integrada nativamente en Tiendanube (no necesitan RPA)**. El RPA solo haría falta para el/los transportistas que no tengan integración nativa (ej. Siempre Logística, si no tiene API/integración con Tiendanube). Hay que revisar esto específicamente antes de asumir que todo el flujo necesita RPA.

- Sí existe un tag para diferenciar tipo de envío (confirmado por Luis) — falta ver los nombres exactos usados hoy en Tiendanube.
- La etiqueta la genera/envía Isa manualmente una vez que confirma la compra — no hay automatismo hoy.

## 5. Cambios y devoluciones
Política publicada en beautyhousemakeup.com/politicas/ (aplica a Beauty House en general; hay condiciones extra para "pedidos especiales" — ver abajo):
- Procesamiento y envío: hasta 5 días hábiles desde confirmación del pedido.
- Devoluciones: dentro de los 7 días corridos desde la recepción, producto en condición original, sin uso, con embalaje intacto.
- Preventa u oferta: no elegibles para devolución monetaria; cambio permitido hasta 5 días hábiles desde la recepción.
- Gastos de envío de la devolución: a cargo del cliente, salvo falla de fabricación o envío incorrecto.
- Reembolsos: 7 a 10 días hábiles desde procesada la solicitud, al mismo medio de pago original (no a terceros).
- Cambios sujetos a disponibilidad de stock — si no hay stock del producto pedido, se ofrece reembolso o crédito.

**Condiciones específicas para "pedidos especiales" (encargos de maquillaje importado):**
- No se permiten devoluciones monetarias de encargos ya formalizados.
- No hay responsabilidad de Beauty House por demoras de Aduana o del transportista.
- Todo producto se revisa obligatoriamente antes de enviarse.
- Beauty House sí asume responsabilidad por daños/golpes visibles ocurridos hasta la llegada al país.
- No se despachan encargos por moto-mensajería externa a la empresa (riesgo de robo/pérdida no cubierto).

No hay tag/nota en el pedido para marcar "cambio en curso" hoy — no existe ese seguimiento.

## 6. FAQs / info que ya comparte Isa
- Es muy probable que haya reglas no escritas ("en la cabeza de Isa") — a relevar con export de chats + entrevista directa.
- Hay clientes recurrentes con condiciones especiales (mayoristas, posibles descuentos fijos) — confirmado por Luis, sin detalle todavía.
- La web ya publica bastante que sirve de FAQ base: proceso de encargo paso a paso (info del producto → presupuesto → aprobación → pago → espera 15-20 días → envío con tracking), medios de pago (transferencia, débito/crédito, QR, Paypal con comisión, efectivo con 20% descuento, Zelle, Binance), y contacto por WhatsApp (+54 9 11 2452-8750) o mail.

## 7. Multicanal
- Isa vende mucho por WhatsApp e Instagram además de Tiendanube — confirmado por Luis.
- El export de ventas muestra "Canal" = Web, Punto de venta, Móvil — no queda claro si las ventas de Instagram/WhatsApp quedan registradas como "Móvil" o si se cargan manual como "Punto de venta". **Confirmar con Isa cómo carga hoy una venta que arranca en WhatsApp/IG.**
- El stock entre canales es manual — es justo la parte del problema que el sincronizador tiene que resolver.

## 8. Permisos y validación
- Propuesta de Luis: definir un umbral (ej. pedidos grandes en volumen o monto) que requiera aprobación manual antes de impactar stock/pedido real; el resto corre automático.
- **Pendiente:** definir el umbral concreto (cantidad de unidades o monto en pesos) — a decidir con Isa.
- Mantenimiento de reglas/tags después del proyecto: Isa y el bot (según Luis) — falta definir el detalle de qué mantiene cada uno.

## 9. Frecuencia y disparadores de sync
- Isa necesita el stock al día todos los días, cada 12 horas — confirmado por Luis.
- Habrá momentos críticos (lanzamientos) donde hace falta sync más agresivo — no se define todavía, queda para más adelante.

## 10. Interfaz de Isa con la IA
- Sigue abierto — a definir en conjunto con Isa y el hermano de Luis (socio en la idea). Ver `/docs/preguntas-reglas-tiendanube.md` sección 10 para las opciones sobre la mesa.

---

## Resumen — respuestas de Luis (2026-08-03)

1. **Tag de "encargo":** no lo define Isa, lo definimos nosotros y ella lo sigue. Propuesta: `encargo`, `encargo-mayorista`, `encargo-minorista`. Falta cerrar la regla de default (campo vacío / valor fuera de regla) antes de implementar — eso sí lo decidimos nosotros, no hace falta preguntarle a Isa.
2. **Plazo de espera:** 7 a 20 días hábiles (rango más amplio que el de 15-20 días publicado para encargos importados — este es el que se va a usar como referencia general).
3. **Uber como envío:** no es un medio oficial, se usa para casos puntuales (cliente VIP, muy cerca, urgente). Cómo se mete en el sistema: como excepción manual, no como un medio de envío automatizado más. Uber no tiene integración con Tiendanube (sin etiqueta ni API), así que el bot/sincronizador no intenta automatizarlo — cuando aplica, el pedido se marca como "envío manual / urgente" y queda a cargo de Isa resolverlo por fuera del flujo, igual que hoy. No entra al alcance del RPA de etiquetas.
4. **Tags de tipo de envío — YA RESUELTO con los datos del CSV, no hace falta preguntarle a Isa.** No existe un campo de "tag" separado para envío en Tiendanube; lo que ella llama tag es directamente el campo **"Medio de envío"** de cada orden. Los valores reales que ya usa (vistos en el export) son: Punto de retiro, Beauty House, MANDO A RETIRAR, Retirado en Punto de Venta, Envío Nube - Correo Argentino Clásico, Envío Nube - Andreani, @Siempre.Logística Servicio Premium. Esa es la lista completa que existe hoy.
5. **Venta que arranca en WhatsApp/IG:** Isa carga una orden de compra en Tiendanube pero **no queda registrada como tal ni modifica/descuenta stock**. Esto confirma el problema de fondo: hoy Tiendanube no es la fuente de verdad del stock, es más un registro suelto. Fase 0 y el sincronizador tienen que resolver esto de raíz.
6. **Aprobación manual:** para el MVP, **toda operación pide aprobación de Isa antes de procesar el "cupón de compra"** (no hay automatización total desde el día uno). Se va ajustando el umbral con el tiempo, a medida que se gane confianza en el sistema.
7. **Clientes mayoristas/condiciones especiales:** no se agrega en el MVP. Queda fuera de alcance por ahora, se suma más adelante si hace falta.
8. **Reglas no escritas de Isa:** por ahora no se identificó ninguna adicional. Queda abierto por si aparece algo durante el relevamiento de FAQs (Fase 1).

## Interfaz de Isa con la IA (resuelve punto 10 de arriba)
Isa confirmó que quiere poder hablarle a la IA por WhatsApp si tiene una duda. Esto inclina la decisión hacia la opción "solo WhatsApp" (hilo separado del de clientes) en vez de sumar un panel web — a confirmar formalmente en la sesión de reglas con Isa y el hermano de Luis, pero ya hay una señal clara de preferencia real de la usuaria final.
