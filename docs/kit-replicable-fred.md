# Kit replicable de Fred

Este documento sirve para repetir el modelo de Fred en otro comercio sin
arrastrar credenciales, datos de Beauty House ni decisiones que pertenecen a
Isa. Se actualiza cuando una decisión se demuestra en producción.

## 1. El producto que estamos replicando

Fred es un asistente de WhatsApp que:

1. responde consultas y asesora con tono de la marca;
2. consulta catálogo, precio y disponibilidad **en vivo**;
3. junta los datos mínimos cuando la persona quiere comprar;
4. pide aprobación humana antes de crear un checkout;
5. deriva excepciones a una persona del negocio;
6. deja trazabilidad de conversaciones, aprobaciones y pagos.

No es una promesa de "automatizar todo". La seguridad proviene de separar lo
que puede resolver solo de lo que requiere una validación humana.

## 2. Arquitectura base

```text
Cliente por WhatsApp
        ↓
Meta WhatsApp Cloud API
        ↓
FastAPI en Railway (bot/app.py)
   ├─ reglas de flujo y seguridad
   ├─ agente conversacional (DeepSeek)
   ├─ búsqueda semántica de productos (Gemini embeddings)
   ├─ Tiendanube: catálogo, stock, precios y checkout
   └─ Supabase/Postgres: chat, estado y pendientes
        ↓
Isa/equipo: aprueba, responde excepciones y observa operación
```

### Fuente de verdad

| Dato | Sistema dueño | Por qué |
|---|---|---|
| Stock, precio, productos, checkout, pedido y pago | Tiendanube | Es el sistema comercial que debe quedar consistente. |
| Conversación, paso de venta y pendientes de aprobación | Supabase/Postgres | Es memoria operativa; no reemplaza una orden. |
| Mensaje de WhatsApp | Meta | Entrega y recibe el canal oficial. |
| Código y despliegue | GitHub + Railway | Versionado y operación del bot. |

## 3. Qué es reutilizable y qué se configura por cliente

### Reutilizable

- Webhook FastAPI, historial de conversación, estados y aprobaciones.
- Conectores de Meta, Tiendanube y Supabase.
- Reglas de seguridad: no inventar, validar stock antes de vender, aprobación
  humana antes de escribir en Tiendanube.
- Panel de operación, webhook de pagos, resumen diario y recordatorios.
- Pruebas locales y casos de evaluación.

### Propio de cada comercio

- Número de WhatsApp, negocio de Meta y plantillas aprobadas.
- Tienda, aplicación OAuth y reglas de Tiendanube.
- Tono, catálogo, políticas vigentes y criterios de recomendación.
- Número/equipo que recibe aprobaciones.
- Datos de pago, logística, devoluciones y condiciones de preventa o encargos.
- Credenciales: se crean nuevas; nunca se copian desde Beauty House.

## 4. Checklist para dar de alta otro cliente

### Antes de conectar nada

- [ ] Definir quién aprueba ventas y quién responde excepciones.
- [ ] Validar catálogo: productos publicados, SKU, variantes, precio y stock.
- [ ] Reunir políticas vigentes y marcar qué información puede cambiar.
- [ ] Definir flujos especiales: encargo, mayorista, preventa, cambios y pagos.
- [ ] Acordar tono y ejemplos reales de atención sin datos privados.

### Integraciones

- [ ] Crear app y número de Meta; generar token de sistema permanente.
- [ ] Crear app privada de Tiendanube y autorizar la tienda correcta por OAuth.
- [ ] Crear base Supabase y aplicar el esquema SQL de Fred.
- [ ] Crear proyecto Railway y cargar secretos solo allí.
- [ ] Configurar webhook Meta y probar recepción/respuesta.
- [ ] Configurar `order/paid` solo luego de que el checkout esté validado.

### Salida controlada

- [ ] Probar saludo, consulta de stock, recomendación, compra, aprobación y pago.
- [ ] Probar que preventa, encargo y mayorista no abren checkout normal.
- [ ] Revisar las primeras conversaciones diariamente.
- [ ] Activar el número público de a poco; no abrir campañas masivas el día uno.

## 5. Lecciones aprendidas con Beauty House

1. **El stock manda.** Una buena respuesta no sirve si recomienda algo oculto o
   agotado. Las recomendaciones filtran producto publicado + stock positivo;
   los sets sorpresa no se ofrecen para una necesidad específica.
2. **La IA no debe decidir límites comerciales.** Las reglas duras viven en
   código: checkout solo tras aprobación, y encargos/preventas/mayorista quedan
   fuera del checkout normal.
3. **Una venta es una conversación, no un formulario rígido.** Fred conserva
   datos ya aportados y solo pide lo que falta; al final muestra un resumen para
   que la clienta corrija.
4. **La información cambiante se confirma.** Datos bancarios, plazos,
   condiciones de encargo y compatibilidades delicadas no se tratan como
   conocimiento permanente.
5. **Los saludos no necesitan IA.** Resolver "hola" y "gracias" localmente
   reduce costo y demora sin empeorar la experiencia.
6. **Los tests masivos se simulan.** Las pruebas locales usan respuestas falsas
   y no consumen IA. Las pruebas reales se hacen de a pocas, con lectura y sin
   crear órdenes hasta que se aprueben.
7. **El canal humano sigue siendo necesario.** La automatización baja carga;
   no elimina el criterio de la persona dueña del negocio.

## 6. Costos y controles de calidad

- Cada consulta comercial puede usar una búsqueda de embeddings y una o más
  llamadas al modelo conversacional; por eso Fred limita el ciclo a cinco
  llamadas y registra el uso en Railway.
- Un saludo o agradecimiento simple se responde sin llamar a proveedores de IA.
- Las pruebas de `tests/` son simuladas: no escriben en Tiendanube ni llaman a
  Meta, Gemini, DeepSeek o Supabase reales.
- `tests/run_fred_live_evals.py --live` sí usa servicios reales de lectura e
  IA. Solo se corre a propósito y en muestras pequeñas.

## 7. Señales para mejorar antes de escalar

No se replica a diez clientes solo porque el bot funciona una vez. Primero se
observa:

- porcentaje de consultas resueltas sin intervención;
- recomendaciones correctas con stock;
- ventas aprobadas vs. abandonadas;
- cantidad y causa de escalaciones;
- correcciones que Isa hace al bot;
- costo promedio por conversación.

Cuando esos datos sean estables en Beauty House, el siguiente producto no es
un portal multi-cliente todavía: es una instalación repetible con esta lista y
una configuración aislada por comercio. Un portal llega después, cuando haya
operación real para varios negocios y necesidades comunes demostradas.
