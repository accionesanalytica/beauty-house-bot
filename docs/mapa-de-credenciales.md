# Mapa de credenciales y accesos

Este documento explica **qué existe y para qué sirve**. Nunca debe contener
tokens, contraseñas, códigos de recuperación ni valores de variables.

## Regla de organización

| Lugar | Para qué se usa |
| --- | --- |
| Gestor de contraseñas | Fuente maestra: cuentas, contraseñas, MFA y claves originales. |
| Railway Variables | Credenciales activas del bot desplegado en producción. |
| `.env` de este proyecto | Copia local para desarrollar y hacer pruebas. Está ignorado por Git. |
| GitHub | Código y documentación; nunca secretos. |

## Servicios del bot

| Servicio | Variable o acceso | Uso | Dónde se administra |
| --- | --- | --- | --- |
| Meta / WhatsApp | `WHATSAPP_TOKEN` | Enviar mensajes mediante Cloud API. | Meta Developers, Railway y gestor de contraseñas. |
| Meta / WhatsApp | `PHONE_NUMBER_ID` | Identifica el número emisor configurado en Meta. No es secreto. | Meta Developers y Railway. |
| Meta / WhatsApp | `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | Comprueba que Meta está verificando nuestro webhook. | Meta Developers, Railway y gestor de contraseñas. |
| Meta / WhatsApp | `APP_SECRET` | Secreto de la aplicación Meta; no lo usa hoy el código activo. | Meta Developers y gestor de contraseñas. |
| Meta / WhatsApp | `WABA_ID` | Identificador de la cuenta de WhatsApp Business. No es secreto. | Meta Developers y Railway. |
| Tiendanube | `TIENDANUBE_ACCESS_TOKEN` | Consultar productos, stock, precios y pedidos vía API. | Portal de Partners, Railway y gestor de contraseñas. |
| Tiendanube | `TIENDANUBE_CLIENT_ID` | Identifica la app OAuth. No es secreto. | Portal de Partners, Railway y gestor de contraseñas. |
| Tiendanube | `TIENDANUBE_CLIENT_SECRET` | Permite intercambiar un código OAuth por un token. | Portal de Partners, Railway y gestor de contraseñas. |
| Tiendanube | `TIENDANUBE_STORE_ID` | Identifica la tienda Beauty House. No es secreto. | Tiendanube y Railway. |
| Supabase | `SUPABASE_DB_URL` | Conexión a Postgres; incluye una contraseña y es secreta. | Supabase, Railway y gestor de contraseñas. |
| Gemini | `GEMINI_API_KEY` | Crear embeddings para identificar productos similares. | Google AI Studio, Railway y gestor de contraseñas. |
| DeepSeek | `DEEPSEEK_API_KEY` | Ejecutar el agente conversacional. | DeepSeek, Railway y gestor de contraseñas. |
| GitHub | Cuenta, MFA y token personal si se usa | Guardar y publicar código. | GitHub y gestor de contraseñas. |
| Railway | Cuenta y MFA | Desplegar el bot y administrar variables. | Railway y gestor de contraseñas. |

## Variables antiguas o para revisar

- `VERIFY_TOKEN`: variable anterior; el código actual usa
  `WHATSAPP_WEBHOOK_VERIFY_TOKEN`.
- `ANTHROPIC_API_KEY`: no la usa el código actual. Revocar y eliminar de
  Railway cuando confirmemos que no tiene otro uso.
- El `.env` de `fred_tiendanube` es un respaldo histórico; no lo lee el bot
  actual ni Railway.

## Cuando se rota una credencial

1. Generar la credencial nueva en el proveedor.
2. Actualizar Railway y el `.env` de este proyecto.
3. Hacer una prueba controlada.
4. Revocar la credencial anterior cuando la prueba funcione.
5. Actualizar la fecha de rotación en el gestor de contraseñas.

## Antes de hacer un commit

- Verificar que `.env` no aparezca en `git status`.
- No pegar secretos en documentación, mensajes, logs ni capturas.
- Usar este mapa para recordar los nombres de variables, no sus valores.
