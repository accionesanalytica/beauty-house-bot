# Evaluación conversacional de Fred

La calidad de un agente de ventas no se demuestra con “1.000 chats inventados”
ni con una única respuesta bonita. Se revisa en dos capas:

1. **Reglas deterministas:** estado de compra, datos, SKU, stock, links y
   escalamiento. Corren automáticamente sin servicios externos.
2. **Conversaciones curadas:** situaciones reales anonimizadas de Beauty House,
   evaluadas contra una rúbrica. Se ejecutan contra DeepSeek/Tiendanube en modo
   de solo lectura y nunca envían un WhatsApp.

## Casos curados

`tests/fred_eval_cases.py` contiene 50 escenarios inspirados en consultas de
asesoría, preventa, seguimiento, pagos, mayorista, logística, cambios y
devoluciones. No contiene nombres, teléfonos, direcciones, importes bancarios
ni transcripciones reales.

## Ejecución

Pruebas locales sin costo ni APIs:

```bash
python -m unittest discover -s tests -v
```

Vista previa de los 50 casos:

```bash
python tests/run_fred_live_evals.py
```

Evaluación real de una muestra, sin enviar mensajes:

```bash
python tests/run_fred_live_evals.py --live --limit 10
```

Antes de usar el modo `--live`, revisar que `.env` tenga las credenciales ya
configuradas. El modo consulta DeepSeek y Tiendanube, por lo que tiene costo de
modelo y usa el catálogo real, pero no escribe ni crea pedidos.

## Cómo leer los resultados

Un `OK automático` solo significa que no disparó una regla crítica. Isa debe
revisar la naturalidad, claridad, utilidad comercial y si una recomendación
efectivamente tiene sentido. Un resultado `REVISAR` no implica que el bot haya
hecho algo irreversible: es una señal para mejorar el prompt, la fuente o la
regla antes de abrir el flujo.

## Regla de apertura controlada

Antes de exponer Fred a más clientas, correr los 50 casos y revisar los que
involucran recomendaciones, preventa, pagos y reclamos. Después habilitarlo
primero para un volumen bajo, revisando diariamente las conversaciones y los
pendientes de Isa.
