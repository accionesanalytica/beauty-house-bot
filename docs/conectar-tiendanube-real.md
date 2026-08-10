# Conectar Tiendanube real sin curl

Fred puede recibir una autorización OAuth de Tiendanube y guardar el token
cifrado en Supabase. Esto evita copiar tokens temporales o usar la tienda demo.

## Requisitos en Railway

- `TIENDANUBE_STORE_ID=2060155` (Beauty House).
- `TIENDANUBE_STORE_DOMAIN=beautyhouse5.mitiendanube.com`.
- `TIENDANUBE_CLIENT_ID` y `TIENDANUBE_CLIENT_SECRET` de la app de Partners.
- `SUPABASE_DB_URL`.
- `TIENDANUBE_CHECKOUT_MODE=production` solo cuando se quiera crear links
  reales tras la aprobación de Isa.

El antiguo `TIENDANUBE_ACCESS_TOKEN` puede mantenerse como respaldo. Fred usa
primero el token OAuth cifrado cuando existe.

## Una sola conexión

1. En Tiendanube Partners, configurar la URL de redirección de la app como:
   `https://TU-DOMINIO-RAILWAY/tiendanube/oauth/callback`.
2. Con la sesión de **Beauty House** iniciada, abrir:
   `https://TU-DOMINIO-RAILWAY/tiendanube/connect`.
3. Aceptar los permisos solicitados.
4. La página final debe indicar que Beauty House quedó conectada. No muestra ni
   registra el token en el navegador o los logs.

Fred rechaza la conexión si el identificador recibido no coincide con
`TIENDANUBE_STORE_ID`; así una autorización de la tienda demo no puede
reemplazar las credenciales de producción.

El inicio de conexión abre el administrador de la tienda indicada en
`TIENDANUBE_STORE_DOMAIN`, no el selector genérico de Partners. Esto evita que
una sesión de la tienda demo vuelva a autorizar el token equivocado.

## Qué se guarda

Solo se crea, al completar la autorización, la tabla
`integration_credentials` en Supabase. El token se guarda cifrado con una clave
derivada del `TIENDANUBE_CLIENT_SECRET`, que permanece en Railway.

Rotar el Client Secret obliga a conectar Tiendanube de nuevo, porque el token
anterior ya no puede descifrarse. Es el comportamiento seguro esperado.
