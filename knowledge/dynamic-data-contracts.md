---
{
  "index": false,
  "document_type": "dynamic_data_contracts",
  "approved_by": "Isa",
  "reviewed_at": "2026-08-11"
}
---
# Dynamic Data Contracts

Estos datos no se indexan ni se congelan en embeddings:

| Dato | Fuente obligatoria | Si falla |
|---|---|---|
| Stock, publicación, variante y precio actual | Tiendanube live | Informar que no pudo verificarse; no inventar |
| Precio/mínimo mayorista exacto | Publicación o presupuesto vigente | Recopilar producto/cantidad y derivar a Isa |
| Checkout y link de pago | Tool de checkout aprobada | No reconstruir URL; mantener flujo de aprobación |
| Estado de orden, pago, tracking | Tiendanube/transportista live | Pedir identificadores faltantes o escalar |
| Horarios disponibles | Calendario live | No confirmar; consultar a Isa |
| Promociones, descuentos, comisiones, recargos, cambio | Fuente comercial vigente | No confirmar |
| Datos bancarios | Canal seguro y fuente vigente | No recuperarlos desde RAG |
| Tarifa actual de courier | Presupuesto vigente con peso/origen/tipo | No calcular con valores históricos |

Un link estático sólo puede salir de metadata aprobada. Un link dinámico sólo puede salir de una herramienta verificada.
