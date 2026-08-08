# Preguntas para definir reglas de negocio en Tiendanube

Checklist para la sesión con Isa (Fase 0). Objetivo: que el sincronizador y el bot después no tengan que adivinar nada.

## 1. Stock real vs. hecho por encargo
- ¿Qué productos son siempre stock físico (se venden solo si hay unidad ya hecha)?
- ¿Qué productos son siempre a pedido/encargo (se hacen cuando se vende)?
- ¿Hay productos mixtos (a veces hay stock, a veces se hacen a pedido)? Si es así, ¿cómo se distingue una venta de otra en ese caso?
- ¿Qué tag/etiqueta se va a usar en Tiendanube para marcar "hecho por encargo"? (definir nombre exacto, ej. `encargo`, `a-pedido`)
- ¿Cuánto tarda en promedio un producto hecho a pedido? (para poder comunicarle un plazo al cliente por WhatsApp)

## 2. Variantes (talles / colores)
- ¿El stock se maneja por variante (talle/color) o a nivel de producto general?
- ¿Hay talles que casi no tienen stock y conviene marcar como "a pedido" directamente?
- ¿Cómo se actualiza el stock de variantes hoy: manual, por planilla, algo automático?

## 3. Estados de pedido
- ¿Qué estados usa Isa hoy en Tiendanube (pendiente, pagado, preparado, enviado, entregado, cancelado)? ¿Usa todos o solo algunos?
- ¿Hay un estado o tag específico para "esperando que se haga el producto" (distinto de "pedido pagado, listo para preparar")?
- ¿Quién cambia el estado del pedido hoy — Isa manualmente, o hay algo semi-automático?

## 4. Envíos y etiquetas
- ¿Todos los envíos van por Mercado Envíos o hay otras modalidades (retiro en persona, envío propio, otro correo)?
- ¿Hay algún tag para diferenciar el tipo de envío (ej. "retira en local" vs "envío a domicilio")?
- ¿En qué momento del flujo se genera/imprime la etiqueta — apenas se paga, o cuando el producto ya está listo (importante si es a pedido)?

## 5. Cambios y devoluciones
- ¿Qué política real aplica hoy (plazo, condiciones, quién paga el envío de vuelta)?
- ¿Se maneja algún tag o nota en el pedido cuando hay un cambio en curso?
- ¿Los cambios de talle afectan el stock de la variante original y la nueva? (relevante para que el sincronizador no descuadre el inventario)

## 6. FAQs / info que ya comparte Isa
- Además de los PDFs que ya tiene, ¿hay reglas que solo existen "en la cabeza de Isa" y nunca se escribieron (excepciones, casos especiales)?
- ¿Hay clientes recurrentes con condiciones especiales (mayoristas, descuentos fijos) que el bot debería reconocer?

## 7. Multicanal
- ¿Isa vende solo por Tiendanube, o también por Instagram/otros canales que después haya que reflejar en el mismo stock?
- Si vende por otro canal, ¿ese stock ya está sincronizado con Tiendanube o es manual?

## 8. Permisos y validación
- Una vez que el bot cree un pedido o el sincronizador toque el stock, ¿alguien tiene que aprobar/revisar antes de que impacte de verdad, o corre automático?
- ¿Quién es responsable de mantener las reglas/tags actualizadas después de este proyecto — Isa sola, o alguien más del local?

## 9. Frecuencia y disparadores de sync
- ¿Cada cuánto necesita Isa que el stock esté al día (tiempo real, cada X horas, una vez al día)?
- ¿Hay momentos críticos (ej. lanzamiento de colección) donde el sync tiene que ser más agresivo?

## 10. Interfaz de Isa con la IA
- ¿Dónde "vive" el ecosistema para el uso diario de Isa? Opciones sobre la mesa:
  - Solo WhatsApp: Isa tiene su propio hilo con el bot (separado del de clientes) y le pregunta/corrige ahí en lenguaje natural. Cero UI nueva.
  - WhatsApp + panel web simple: WhatsApp para hablar con la IA, más una página mínima para ver pedidos/stock de un vistazo.
- **Señal ya confirmada (2026-08-03):** Isa dijo que quiere poder hablarle a la IA por WhatsApp si tiene dudas. Inclina fuerte hacia "solo WhatsApp". Falta cerrarlo formalmente con Isa y el hermano de Luis, pero ya no es una pregunta completamente abierta.
- Si igual se suma panel web más adelante: ¿quién más lo usaría además de Isa? ¿Hace falta login/roles o alcanza con acceso simple?

---
**Resultado esperado de esta sesión:** lista cerrada de tags/estados con su significado exacto, para documentar en `/docs/reglas-tiendanube.md` antes de tocar código.
