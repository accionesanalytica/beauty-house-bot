# Operación de Fred

Este documento describe la operación real. No contiene contraseñas ni tokens.

## Qué hace Fred hoy

1. Atiende consultas y recomendaciones con catálogo/stock vigente de Tiendanube.
2. Si una clienta elige un producto normal, junta producto, cantidad, entrega y contacto.
3. Isa aprueba la ficha antes de que Fred cree un checkout. Tiendanube es quien reserva stock y registra el pago/pedido.
4. Un encargo, preventa, cotización especial o venta mayorista **no** se convierte en checkout normal: se deriva a Isa y usa las condiciones vigentes.
5. Isa puede responder mediante Fred desde su WhatsApp usando **Responder a Fred**. Fred entrega ese dato a la clienta y retoma el chat.

## Panel privado

Ruta: `https://<dominio-de-fred>/admin`

Se protege con autenticación de usuario y contraseña. Configurar en Railway:

- `ADMIN_DASHBOARD_USERNAME`: un usuario exclusivo para Isa/equipo.
- `ADMIN_DASHBOARD_PASSWORD`: contraseña larga y exclusiva (guardar en el gestor de contraseñas).

El panel permite ver conversaciones, estados, pendientes, checkouts aprobados y pagos confirmados en las últimas 24 horas. No reemplaza Tiendanube para gestión de pedidos.

## Pago confirmado por Tiendanube

Fred puede recibir el evento `order/paid` de Tiendanube. Cada evento se valida con la firma de la app y se guarda una sola vez aunque Tiendanube lo reintente.

Para activarlo después del deploy:

1. En Railway, agregar `TIENDANUBE_WEBHOOKS_ENABLED=true` y desplegar.
2. Entrar al panel privado `/admin` y pulsar **Conectar avisos de pago**. Fred registra una vez el evento `order/paid`; no hay que usar terminal ni copiar tokens.
3. Hacer un checkout de prueba de bajo valor y verificar el resultado en `/admin`.

El webhook verifica pago y lo muestra en el panel. Para avisar automáticamente a una clienta pagada, Meta exige una plantilla aprobada porque el pago puede llegar fuera de la ventana de 24 horas.

Cuando exista esa plantilla, agregar en Railway:

- `PAYMENT_CONFIRMED_TEMPLATE_NAME`: nombre exacto de la plantilla aprobada.
- `PAYMENT_CONFIRMED_TEMPLATE_LANGUAGE`: `es_AR` salvo que Meta muestre otro código.

La primera versión de esa plantilla debe tener **una variable de cuerpo**: el número de orden. Ejemplo de texto:

> ¡Gracias por tu compra! Recibimos el pago de tu pedido #{{1}}. Beauty House te va a avisar cuando haya novedades de preparación o envío.

No activar estas variables antes de que Meta apruebe la plantilla.

## Rutina de Isa

- `ver`: muestra el próximo pendiente.
- `Responder a Fred`: Isa escribe una respuesta concreta; Fred se la entrega a la clienta y retoma.
- `Aprobar compra`: crea el checkout normal solo después de revisar la ficha.
- `Enviar condiciones`: envía el PDF vigente de encargos/preventas; no crea cobro ni reserva.
- `resumen`: envía a Isa el conteo actual de Fred.
- `calidad`: muestra casos de hoy que conviene revisar (escalaciones,
  solicitudes de ayuda y compras pendientes). Es un reporte a demanda; no crea
  una alerta adicional ni usa IA.
- `recordame en 1 hora` / `no me recuerdes`: controla los recordatorios de pendientes.

## Resumen automático de las 21:00

Fred ya tiene el programador preparado para una plantilla Meta de **cuatro variables**:

1. conversaciones atendidas;
2. checkouts aprobados;
3. pagos confirmados;
4. pendientes para Isa.

No se activa hasta que Meta apruebe la plantilla. Cuando esté aprobada, agregar en Railway:

- `DAILY_SUMMARY_ENABLED=true`
- `DAILY_SUMMARY_TEMPLATE_NAME`: nombre exacto de la plantilla.
- `DAILY_SUMMARY_TEMPLATE_LANGUAGE=es_AR`

Fred la envía una sola vez por día después de las 21:00 de Argentina. Si Railway se reinicia, no duplica el reporte.

## Antes de abrir el número al público

- Probar un checkout normal de bajo valor y verificar el pago en Tiendanube + panel.
- Revisar una venta mayorista y un encargo para confirmar que no se convierten en compra normal.
- Confirmar que el perfil de WhatsApp del número oficial tiene nombre, foto y descripción de Beauty House.
- Configurar coexistencia del número oficial solo cuando llegue el eSIM, para que Isa pueda observar/continuar chats desde WhatsApp Business.
