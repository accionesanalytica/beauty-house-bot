# Checkout real aprobado por Isa

## Flujo de venta normal

1. Fred verifica producto, variante, precio y disponibilidad actuales.
2. Reúne cantidad, envío/retiro, nombre y email, y muestra el resumen.
3. La clienta confirma; Isa recibe una tarjeta **Aprobar compra**.
4. Al aprobar, Fred consulta de nuevo el SKU y el stock real en Tiendanube.
5. Si sigue vendible, crea un **borrador no pagado** y envía su checkout a la
   clienta. Ella completa dirección o retiro y el medio de pago dentro de
   Tiendanube.
6. Tiendanube crea/registra la orden cuando la clienta completa el checkout.

Fred no descuenta stock mediante una segunda llamada. Tiendanube conserva la
fuente de verdad del pedido y del inventario; una doble modificación sería un
riesgo de sobreventa.

## Interruptor de seguridad

En Railway, el modo permanece apagado por defecto:

```text
TIENDANUBE_CHECKOUT_MODE=disabled
```

Cuando se haya hecho una prueba controlada de punta a punta con una venta de
prueba que Isa pueda cancelar, cambiarlo a:

```text
TIENDANUBE_CHECKOUT_MODE=production
```

El token de Tiendanube necesita como mínimo `read_products` y
`write_draft_orders`. No guardar el token en este archivo ni en Git.

## Qué pasa si algo falla

- Stock insuficiente, producto oculto o SKU inválido: Fred no crea el link y
  deja el pendiente abierto para Isa.
- La creación funciona pero WhatsApp no entrega el link: el checkout queda
  guardado dentro del pendiente; al volver a aprobar, Fred reenvía el mismo
  link y no genera otro.
- El checkout pide dirección/retiro y pago: eso se hace en Tiendanube, no por
  WhatsApp.

## Pendiente posterior

El mensaje automático **después del pago** requiere configurar el webhook
`order/paid` de Tiendanube y asociarlo con este checkout. No se habilita hasta
probar que el pedido, stock y cobro quedan como espera Isa.
