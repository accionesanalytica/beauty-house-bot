---
{
  "id": "facts-pickups-showroom-v1",
  "topic": "pickups_showroom",
  "knowledge_type": "fact",
  "source": "Isa: Operación comercial + retiros",
  "approved_by": "Isa",
  "reviewed_at": "2026-08-11",
  "valid_until": "2026-09-11",
  "risk_level": "high",
  "requires_isa_confirmation": false,
  "keywords": ["retiro", "showroom", "Vidal", "cadete", "moto", "otra persona", "reserva", "calendario"],
  "required_disclosures": [
    {"id": "showroom-closed", "text": "El showroom está cerrado para atención al público hasta nuevo aviso; actualmente sólo se realizan retiros coordinados con reserva previa en Vidal 2680.", "required_terms": ["showroom", "cerrado", "reserva previa"], "when_any": ["showroom", "visitar", "atención al público"]}
  ],
  "required_links": [
    {"id": "pickup-calendar", "link_type": "approved_static_link", "url": "https://calendar.app.google/Y5kYYhQtuQn8JTYU8", "when_any": ["retiro", "retirar", "reserva", "agendar", "calendario", "cadete", "moto"]}
  ]
}
---
# Showroom y retiros

## Atención presencial

El showroom está cerrado para atención al público hasta nuevo aviso. Actualmente sólo se realizan retiros previamente coordinados en Vidal 2680. No se ofrece atención espontánea.

## Retiro por otra persona

Puede retirar otra persona con reserva previa, número de orden, nombre y apellido y DNI de la persona autorizada. La titular puede dejar autorización escrita desde el mismo WhatsApp registrado en la orden. El correo usado para reservar debe coincidir con el de la compra.

## Moto o cadete enviado por la clienta

La clienta puede enviar una moto o cadete para retirar, con reserva, número de orden, nombre del conductor, patente y autorización escrita. Beauty House deja de ser responsable una vez entregado el paquete al tercero autorizado. Beauty House no ofrece despachos externos de moto contratados por la clienta; sólo admite que la clienta envíe un tercero a retirar bajo estas condiciones.

## Horarios

La disponibilidad real de horarios se consulta en el calendario. Si el horario buscado no aparece, Fred recopila día y hora y consulta a Isa sin confirmarlo.
