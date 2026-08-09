# Ventas externas — guía corta para Isa

Isa no necesita recordar comandos. Puede escribirle a Fred de forma normal,
por ejemplo:

> “Vendí unos productos por Instagram, ¿me armás el link?”

Fred pregunta cómo se debe clasificar la venta y ofrece cuatro opciones:

1. **Venta normal** — producto con stock físico.
2. **Encargo** — producto que se pide y no se promete como stock inmediato.
3. **Venta mayorista** — condición comercial especial.
4. **Otro** — caso que no encaja; Fred pide una explicación antes de hacer nada.

Después de elegir, Isa envía en un mensaje producto, variante, cantidad y los
datos que tenga de la clienta. Fred guarda un borrador interno para revisión.

## Seguridad actual

Este flujo **todavía no crea** pedidos, links de pago, tags ni movimientos de
stock. Solo registra el tipo de operación y los detalles para preparar la
siguiente fase de aprobación.

Cuando se habiliten órdenes reales:

- `encargo` será un tag de orden;
- `venta_mayorista` será un tag de orden;
- la venta normal usará stock físico;
- Isa deberá aprobar antes de que Fred cree un checkout real.
