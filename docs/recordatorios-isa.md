# Recordatorios de Fred para Isa

Fred acompaña la operación; no persigue a Isa ni toma decisiones comerciales por
ella.

## Rutina automática

Cuando hay al menos un pendiente sin resolver:

1. Al entrar el pendiente, Isa recibe la plantilla de aviso habitual.
2. Si sigue pendiente a los 25 minutos, Fred manda un recordatorio suave.
3. A las 2 horas, manda un segundo recordatorio.
4. No envía recordatorios automáticos entre las 20:30 y las 10:00 de Argentina.
5. Cada tipo de recordatorio se manda, como máximo, una vez por día. Nunca se
   crean compras, links ni descuentos desde un recordatorio.

El aviso usa la plantilla ya aprobada `escalacion_isa`, por lo que puede llegar
incluso si la ventana de 24 horas de WhatsApp no está abierta.

## Frases que Isa puede usar por WhatsApp

- `recordame en 1 hora`
- `no me recuerdes hoy`
- `reactivá los recordatorios`
- `ver` para abrir el siguiente pendiente.

Fred confirma lo que entendió con un tono amable. Si no hay pendientes, lo dice
sin generar otra tarea.

## Activación

1. Ejecutar [003_isa_reminders.sql](sql/003_isa_reminders.sql) en Supabase.
2. En Railway, agregar `ISA_REMINDERS_ENABLED=true`.
3. Reiniciar/desplegar y probar con un pendiente de ejemplo.

## Casos límite

- Si Isa descarta o aprueba, el pendiente desaparece y no hay más avisos.
- Si Meta rechaza temporalmente el mensaje, el sistema libera la reserva de ese
  recordatorio para poder reintentarlo después.
- Si Railway se reinicia, los horarios y silencios sobreviven porque están en
  Supabase.
- Si Isa pide un recordatorio explícito, ese pedido respeta la hora que ella
  eligió; los avisos automáticos permanecen silenciados hasta entonces.
