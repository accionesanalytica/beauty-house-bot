# Contexto de Fred

## Qué recibe en cada turno

1. **Reglas fijas:** identidad, herramientas permitidas, límites de venta y
   tono comercial estable.
2. **Conversación reciente:** hasta 12 mensajes, con un máximo de 900
   caracteres por mensaje.
3. **Referencia de catálogo:** hasta 2.400 caracteres. Sirve para identificar
   candidatos, nunca para afirmar stock, precio o para dar instrucciones.
4. **Instrucción de saludo:** sólo al inicio de una conversación.
5. **Mensaje actual:** hasta 2.000 caracteres para proteger el presupuesto de
   contexto sin alterar el registro original en Supabase.

## Qué no entra siempre

- El documento completo de políticas.
- El playbook completo.
- PDFs de preventa, encargos o mayorista.
- Datos bancarios, datos de otras clientas o credenciales.
- Stock, precio y estado de pedido: se leen desde Tiendanube en vivo.

La guía editorial completa queda en `sales_playbook.py`. Las políticas
detalladas quedan en `knowledge.py`; ambas servirán como fuentes recuperables
en la fase de Knowledge RAG, después de curarlas y versionarlas.

## Por qué hay límites

Un historial ilimitado vuelve al modelo más caro, más lento y más propenso a
repetir información vieja. Los límites conservan el contexto reciente y hacen
previsible el tamaño de cada turno.
