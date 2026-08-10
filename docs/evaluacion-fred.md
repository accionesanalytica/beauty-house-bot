# Evaluación de Fred

## Propósito

La evaluación es la puerta de calidad de Fred: permite detectar regresiones
antes de desplegar cambios de prompt, retrieval, herramientas o flujos de
venta. No reemplaza el criterio de Isa para tono o casos comerciales nuevos.

## Tres capas actuales

1. **Tests deterministas**: validan reglas de venta, escalación, checkout,
   idempotencia y seguridad sin usar un modelo.
2. **Harness del webhook**: ejecuta el recorrido de un mensaje de WhatsApp por
   `app.webhook_post` con dobles locales de Meta, DeepSeek, Tiendanube y
   Supabase. Nunca envía mensajes ni genera coste.
3. **Casos live opcionales**: `run_fred_live_evals.py --live` consulta
   DeepSeek y Tiendanube sólo en lectura. Se usa con una muestra pequeña y
   revisión humana; no es parte de la suite normal.

## Criterios de salida actuales

- Ningún checkout se crea sin aprobación de Isa.
- Un producto oculto, agotado o sin verificación live no se ofrece como
  disponible.
- Una URL no verificada se elimina de la respuesta.
- Un mensaje Meta duplicado no recibe una segunda respuesta.
- Fallas de servicio se degradan a un mensaje seguro o escalación, no a una
  respuesta inventada.

## Cómo ejecutar

Desde la raíz del proyecto, con el entorno virtual local:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

La ejecución normal no usa credenciales de producción ni toca servicios
externos. Los casos live se ejecutan sólo de forma deliberada y nunca envían
WhatsApps.

## Límite conocido

El evaluador live actual llama al agente directamente. En fases posteriores se
ampliará para probar retrieval, memoria y ficha de venta como un flujo completo.

La suite de harness verifica rutas deterministas que existen hoy. Los casos de
lenguaje natural que todavía dependen de DeepSeek —por ejemplo, algunas
variantes de “quiero hablar directamente con Isa”— permanecen en los casos de
evaluación y son una brecha a cerrar en la fase de guardrails, no una prueba de
que esa detección ya sea determinista.
