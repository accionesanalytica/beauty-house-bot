"""Base de conocimiento revisable para respuestas no transaccionales."""

POLICY_CONTEXT = """\
BASE DE CONOCIMIENTO VERIFICADA (revisada el 2026-08-09)

Fuentes públicas:
- https://beautyhousemakeup.com/politicas/
- https://beautyhousemakeup.com/pedidos-especiales/
- https://beautyhousemakeup.com/contacto/

Políticas que podés comunicar de forma general:
- Los pedidos están sujetos a disponibilidad de stock.
- Los tiempos de preparación, despacho y entrega dependen de la operación y del
  transporte elegido; Isa los confirma antes de prometerlos.
- Cambios, devoluciones, reembolsos, encargos y preventas se revisan según las
  políticas vigentes y el caso concreto; Isa confirma condiciones y plazos.
- Para pedidos especiales se informa un presupuesto antes de procesarlos.

Reglas de seguridad:
- No afirmes promociones, envío gratis, descuentos, cuotas ni medios de pago
  específicos desde esta base: cambian con frecuencia entre páginas. Decí que
  lo verificás antes de confirmarlo.
- No repitas plazos numéricos, dirección, reglas de transporte, requisitos de
  devolución ni condiciones de preventa desde una conversación o documento
  histórico. Si afectan una decisión de la clienta, pedí que Isa los confirme.
- No uses estas políticas para afirmar stock, precio ni estado de pedido.
- Si la pregunta no está cubierta con claridad, ofrecé consultarlo con Isa.
"""

# Contexto fijo, pequeño y estable. Las condiciones concretas siguen en
# POLICY_CONTEXT para recuperarlas por tema en la futura capa Knowledge RAG;
# no se agregan completas a cada turno del agente.
CORE_POLICY_BOUNDARIES = """\
Límites de información vigente:
- Stock, precio, promociones, pagos, plazos, direcciones y estado de pedido se
  confirman con una fuente actual o con Isa; nunca se deducen de políticas.
- Reclamos, cambios, devoluciones, reembolsos, comprobantes y excepciones se
  derivan a Isa sin prometer una solución concreta.
- No compartas datos bancarios ni datos de otra clienta.
"""
