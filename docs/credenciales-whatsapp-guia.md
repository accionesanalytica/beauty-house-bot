# Guía: obtener las credenciales de WhatsApp Cloud API

Requisito previo: negocio verificado en Meta Business Manager. ✅ (hecho 2026-08-07)

## ⚠️ Seguridad — leer primero

- **No pegar estos valores en chats, ni conmigo ni con nadie.** Son equivalentes a una contraseña: con el token se puede mandar mensajes en nombre del negocio.
- **Nunca subirlos a GitHub.** Van en un archivo `.env` local y ese archivo va listado en `.gitignore`.
- En producción van cargados como *variables de entorno* en Railway, no dentro del código.
- Si alguna vez se filtra un token, se revoca desde Meta y se genera uno nuevo.

## Qué es cada cosa

| Variable | Qué es | De dónde sale |
|---|---|---|
| `VERIFY_TOKEN` | Contraseña que inventás vos para que Meta confirme que el webhook es tuyo | **La inventás vos**, no viene de Meta |
| `APP_SECRET` | Clave secreta de la app, sirve para validar que los mensajes vienen de Meta | Panel de la app → Configuración → Básica |
| `WHATSAPP_TOKEN` | Token de acceso para enviar/recibir mensajes | System User en Business Manager (permanente) |
| `PHONE_NUMBER_ID` | Identificador del número de WhatsApp del bot | WhatsApp → Configuración de la API |

También va a hacer falta el **WABA ID** (WhatsApp Business Account ID), que aparece en la misma pantalla que el Phone Number ID.

---

## Paso 1 — Crear la app en Meta for Developers

1. Entrar a **developers.facebook.com** con la cuenta de Isa.
2. *Mis Apps* → **Crear app**.
3. Tipo de app: **Empresa / Business**.
4. Vincularla al portfolio comercial verificado (el que ya verificaron).
5. En el panel de la app: **Agregar producto → WhatsApp → Configurar**.

## Paso 2 — PHONE_NUMBER_ID (y WABA ID)

En el panel de la app: **WhatsApp → Configuración de la API** (API Setup).

Ahí aparecen:
- **Identificador del número de teléfono** → ese es el `PHONE_NUMBER_ID`
- **Identificador de la cuenta de WhatsApp Business** → ese es el `WABA_ID`

Meta da un número de prueba gratis para desarrollar. El número real de Isa se agrega después con "Agregar número de teléfono" (ojo: ese número **no puede tener WhatsApp normal ni WhatsApp Business app activo** — hay que darlo de baja antes).

## Paso 3 — APP_SECRET

Panel de la app → **Configuración → Básica** → campo **Clave secreta de la app** → botón *Mostrar* (pide la contraseña de Facebook).

## Paso 4 — WHATSAPP_TOKEN permanente (el más importante)

En "Configuración de la API" Meta muestra un token temporal que **vence a las 24 horas**. No sirve para producción. Hay que generar uno permanente vía System User:

1. Ir a **business.facebook.com** → **Configuración del negocio**.
2. Menú izquierdo → **Usuarios → Usuarios del sistema**.
3. **Agregar** → nombre (ej. `shoow-bot`) → rol **Administrador**.
4. Seleccionar el usuario creado → **Asignar activos**:
   - Elegir la **app** creada en el paso 1 → activar **Controlar la app** (control total).
   - Elegir la **cuenta de WhatsApp** → activar **Administrar cuentas de WhatsApp Business** (control total).
5. Botón **Generar token**:
   - Elegir la app.
   - Vencimiento: **Nunca**.
   - Marcar los permisos: `whatsapp_business_messaging` y `whatsapp_business_management`.
6. Copiar el token **en ese momento** — Meta lo muestra una sola vez. Si se pierde, se genera otro.

## Paso 5 — VERIFY_TOKEN

Este no se busca en ningún lado: **lo inventás vos**. Es una palabra secreta que vas a escribir en dos lugares (en el código del bot y en la configuración del webhook en Meta), y Meta la usa para confirmar que el webhook te pertenece.

Que no sea adivinable. En vez de `shoow_bot_2026`, mejor algo aleatorio. Se puede generar con este comando en la terminal:

```
openssl rand -hex 24
```

## Paso 6 — Guardarlas

Crear un archivo `.env` en la raíz del proyecto (nunca subirlo a GitHub):

```
VERIFY_TOKEN=<el que generaste vos>
APP_SECRET=<paso 3>
WHATSAPP_TOKEN=<paso 4, el permanente>
PHONE_NUMBER_ID=<paso 2>
WABA_ID=<paso 2>
```

Y agregar `.env` al archivo `.gitignore`.

Cuando llegue el momento de desplegar, esas mismas cuatro/cinco variables se cargan en Railway (pestaña *Variables* del proyecto).

## Paso 7 — Webhook (esto viene después)

El webhook recién se configura cuando el bot esté corriendo en Railway y tenga una URL pública. En ese momento: panel de la app → WhatsApp → Configuración → Webhooks → pegar la URL de Railway + el `VERIFY_TOKEN`, y suscribirse al evento `messages`.

---

Fuentes: [WhatsApp Cloud API Get Started — Meta for Developers](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started) · [Using Authorization Tokens for the WhatsApp Business Platform](https://developers.facebook.com/blog/post/2022/12/05/auth-tokens/)
