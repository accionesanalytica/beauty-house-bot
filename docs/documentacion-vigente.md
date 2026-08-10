# Documentación vigente y archivo histórico

El proyecto evolucionó rápido. Algunos documentos explican decisiones iniciales
que ya fueron reemplazadas; se conservan como historia, pero no deben guiar un
deploy ni una venta real.

## Leer primero (vigente)

| Documento | Para qué sirve |
|---|---|
| `operacion-fred.md` | Operación real: checkout, Isa, pagos, resumen y número oficial. |
| `kit-replicable-fred.md` | Arquitectura actual y cómo instalar Fred en otro comercio. |
| `checkout-real.md` | Límites y flujo seguro del checkout aprobado. |
| `conversation-robustness.md` | Cómo Fred conserva una selección y recopila datos sin formularios vacíos. |
| `conectar-tiendanube-real.md` | Conexión OAuth a Beauty House sin usar terminal. |
| `mapa-de-credenciales.md` | Dónde vive cada acceso; nunca contiene valores secretos. |
| `evaluacion-conversacional-fred.md` y `qa-fred-mvp.md` | Cómo probar calidad sin gastar servicios reales. |

## Históricos: útiles como contexto, no como instrucción actual

- `README.md`, `arquitectura.md` y `estado-para-hermano.md` describen fases y
  alternativas del arranque. Algunas mencionan Node/Express o decisiones que
  ya no son el stack desplegado.
- `prompt-para-otra-ia.md`, `reglas-tiendanube.md`,
  `plan-skus-y-reconciliacion.md` y `runbook-reconciliacion.md` sirven para
  limpieza de catálogo/inventario. No autorizan modificar stock sin validar el
  conteo y las reglas actuales con Isa.
- `decisiones-tecnicas.md` conserva investigación y notas de trabajo. Puede
  contener decisiones superadas y además se actualiza por separado; no se
  reescribe automáticamente.

## Regla práctica

Si una instrucción afecta una clienta, un checkout, stock, una credencial o el
despliegue, contrastarla primero con `operacion-fred.md` y con el código
desplegado. Si siguen en conflicto, se detiene y se documenta la decisión nueva
antes de ejecutar el cambio.
