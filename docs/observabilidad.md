# Observabilidad de Fred (Fase 7)

Cada turno que llega al agente queda registrado como un evento técnico. El
objetivo es entender la calidad y el costo operativo sin convertir la base de
datos en una copia de los chats.

## Qué se registra

- Acción y motivo decidido (`reply`, `clarify_product`, `start_sales_intake`,
  `handoff_to_isa` o `service_fallback`).
- Resultado del envío (`replied`, `queued_for_isa`, `send_failed` o
  `service_fallback`).
- Herramientas utilizadas, sin sus argumentos.
- Si hubo contexto de catálogo o de conocimiento.
- Cantidad de llamadas al modelo, tokens informados y duración total.
- Identificadores internos para no duplicar eventos si Meta reintenta un
  webhook.

## Qué no se registra

No se guardan teléfono, nombre, correo, texto de la clienta, respuesta completa
de Fred, precios, direcciones, credenciales, tokens ni la URL de la base.

## Seguridad operativa

El registro es secundario: si Supabase no está disponible, Fred igual responde
o usa su respuesta segura. La tabla se crea automáticamente al primer evento;
el script [005_fred_turn_observations.sql](sql/005_fred_turn_observations.sql)
deja el mismo esquema explícito para control de cambios.

La función `agent_observability_snapshot()` entrega solamente agregados de las
últimas 24 horas. La próxima fase puede usar esos agregados en el panel, sin
exponer conversaciones completas.
