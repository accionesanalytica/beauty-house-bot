# Fase 6 — Evaluación completa

La evaluación de Fred combina tres niveles:

1. **Unitarios y webhook:** no usan redes; prueban reglas, estados y límites.
2. **Casos curados:** más de 60 situaciones anonimizadas con expectativa,
   herramientas requeridas, texto prohibido y nota de revisión humana.
3. **Muestra live controlada:** consulta DeepSeek y Tiendanube en modo lectura,
   nunca WhatsApp ni órdenes. Está limitada a diez casos para controlar costo.

## Rúbrica automática

Cada caso live se evalúa sobre 100:

- una frase prohibida, un link no verificado o falta de escalación estructurada
  en un caso sensible resta 45 puntos;
- faltar una herramienta requerida o una acción esperada resta 20 puntos.

El puntaje no certifica que la recomendación sea buena: tono, empatía y
conveniencia comercial requieren revisión de Isa. Sí permite detectar una
regresión objetiva antes de abrir el bot a más conversaciones.

## Criterio sugerido de apertura

1. Ejecutar primero los tests offline.
2. Ejecutar una muestra live de 10 casos variados.
3. No abrir a más clientas si aparece un hallazgo crítico.
4. Revisar manualmente cada respuesta de asesoría, venta, preventa, reclamo y
   encargo, aunque tenga puntaje 100.
