# Fase 4 — Decisiones estructuradas

## Problema

Un modelo puede escribir una frase razonable pero omitir una acción, pedir una
escalación sin justificación o intentar iniciar una compra sin una variante
verificada. Interpretar esos matices sólo desde texto libre hace frágil el
flujo de venta.

## Contrato de decisión

El agente puede proponer, mediante `set_turn_decision`, una de estas acciones:

- `reply`: respuesta informativa sin cambio operativo;
- `clarify_product`: falta identificar un producto;
- `start_sales_intake`: abrir ficha de compra;
- `handoff_to_isa`: crear pendiente para Isa.

El código valida el resultado y construye la decisión efectiva. La propuesta
del modelo no basta para una acción sensible:

| Acción efectiva | Evidencia que exige el código |
| --- | --- |
| `start_sales_intake` | `select_sale_candidate` con SKU y producto, después de `get_stock` en stock en el mismo turno. |
| `handoff_to_isa` | `request_isa_handoff` registrado en el turno o una regla determinista de la aplicación. |
| `clarify_product` | agotamiento técnico de búsqueda o necesidad concreta de identificar el producto. |
| `reply` | opción segura por defecto. |

## Resultado

La lógica de checkout y la cola de Isa siguen en `app.py`, pero ahora reciben
un resultado explícito y auditable. Si la estructura está rota o contradice los
hechos, Fred no inicia venta ni escala por error: responde normalmente o pide
una aclaración.
