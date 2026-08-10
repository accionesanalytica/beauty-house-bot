# Fase 5 — Validación y guardrails

Los prompts orientan al modelo; los guardrails de esta fase imponen límites en
código antes de llamar Tiendanube o de enviar texto por WhatsApp.

## Límites actuales

| Capa | Regla |
| --- | --- |
| Herramientas por ronda | Máximo 4. |
| Herramientas por turno | Máximo 8. |
| Duplicados | La misma herramienta con los mismos argumentos sólo se ejecuta una vez por turno. |
| Búsqueda | Query de 2 a 160 caracteres; límite entre 1 y 5. |
| SKU | Texto breve, sin saltos de línea. |
| ID de producto y orden | Formato y rango comprobados antes de consultar Tiendanube. |
| Escalación / ficha | Razón, resumen y campos requeridos validados. |
| Respuesta | Máximo 1.500 caracteres, cortada por oración si hace falta. |

## Falla segura

Si una herramienta recibe argumentos inválidos, se repite o falla, Fred recibe
un resultado seguro que le indica no inventar datos. Los detalles técnicos de
proveedores o de base de datos no se reenvían al modelo.

Estos límites no sustituyen la aprobación de Isa ni la comprobación en vivo de
stock/precio: sólo reducen acciones accidentales, repeticiones y salidas
malformadas.
