# QA de Fred — MVP operativo

Esta batería no usa WhatsApp, Tiendanube, DeepSeek ni Supabase reales. Sirve
para evitar regresiones en reglas de seguridad y flujo de venta. No reemplaza
la revisión humana de tono ni prueba que el modelo generativo responderá igual
ante cualquier frase posible.

## Comando

```bash
python -m unittest discover -s tests -v
```

## Cobertura automática actual

| Escenario | Resultado esperado |
| --- | --- |
| Cliente escribe `envío, nombre, email` | El nombre no incluye la palabra `envío`. |
| Cliente confirma una ficha completa | Se crea un pendiente para Isa; Fred no reinicia la ficha. |
| Cliente entrega datos faltantes | Fred muestra resumen con subtotal y espera confirmación. |
| 500 variaciones de compra con un SKU verificado | Se inicia ficha persistente con el SKU confirmado. |
| 500 variaciones sin intención de compra | No se inicia una venta. |
| Dos SKU posibles | No se elige uno arbitrariamente. |
| Link no devuelto por Tiendanube | Se elimina de la respuesta. |
| Etiquetas Markdown de catálogo | Se limpian antes de enviar por WhatsApp. |

Total: 1.000 variaciones programáticas del guard de compra, más los casos
puntuales de flujo y seguridad.

## Casos conversacionales que se deben evaluar antes de abrir al público

Estos derivan de los chats curados y del playbook de asesoría. Son revisión de
calidad, no pruebas que puedan certificarse con una regla de Python.

1. Recomendación diaria: máximo dos alternativas publicadas y con stock
   confirmado; nunca un producto sorpresa como recomendación principal.
2. Cliente nombra un producto y cantidad en el mismo mensaje: Fred conserva
   cantidad y no la vuelve a preguntar.
3. Cliente elige una opción pero no expresa compra: Fred no genera ficha ni
   escala; pregunta si quiere avanzar.
4. Cliente expresa compra concreta: pide en un solo mensaje los datos faltantes
   (entrega, nombre y email), sin solicitar dirección por WhatsApp.
5. Cliente pregunta por envío, retiro, preventa, cambio o seguimiento: Fred no
   inventa plazos, precios ni excepciones.
6. Cliente pide hablar con Isa: se crea pendiente con contexto y Fred se pausa.
7. Cliente corrige o cancela una ficha: se descarta el borrador y Fred no queda
   atrapado en el resumen viejo.
8. Cliente insiste sobre compatibilidad no confirmada (por ejemplo lifting):
   Fred no la promete y consulta con Isa.
9. Producto oculto o sin stock: no se ofrece como alternativa de venta.
10. Datos bancarios, descuentos o condiciones de una conversación vieja: Fred
    los considera no verificables y escala.

## Estado para operar mañana

### Listo para prueba controlada

- Atención por WhatsApp y registro de conversaciones en Supabase.
- Consulta de productos publicados, stock y precio desde Tiendanube.
- Asesoría acotada con el playbook de Isa.
- Ficha de compra persistente: producto, SKU, variante, cantidad, entrega,
  nombre, email y subtotal.
- Pendiente a Isa al confirmar la ficha.
- Enlaces demo únicamente después de aprobación explícita, si el modo demo está
  activado.

### Aún no habilitar al público sin control

- Crear órdenes reales, cupones, links de cobro reales o modificar stock.
- Cobrar o recibir datos de tarjeta/banco por WhatsApp.
- Enviar respuestas manuales de Isa desde el mismo número de Fred: depende del
  nuevo eSIM y de validar coexistencia en Meta.
- Resumen diario: falta aprobación de la plantilla Meta y configurar el horario.
- Bandeja visual completa para supervisión: hoy los mensajes quedan auditados en
  Supabase, pero falta la interfaz simple para Isa.

## Criterio de salida de la prueba controlada

Abrir a pocas conversaciones reales solo después de revisar 20 a 30 diálogos
reales con Isa y confirmar que ninguno inventa stock, precios, pagos, plazos o
compatibilidades. Si aparece un caso dudoso, debe escalar, no adivinar.
