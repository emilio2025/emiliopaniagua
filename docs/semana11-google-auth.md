# Inicio de sesión con Google y nombre del cliente en el recibo

## Qué hace
- La cabecera de la tienda muestra **"Iniciar sesión con Google"** (Google Identity Services).
- Para **confirmar una compra** desde la tienda hay que iniciar sesión.
- El navegador envía el **ID token de Google** como `Authorization: Bearer …`. El backend (`backend/app/auth.py`) verifica **firma (JWKS de Google), emisor, audiencia = GOOGLE_CLIENT_ID, expiración y correo verificado**.
- El pedido guarda el cliente en la tabla `order_customers` (misma transacción del pedido).
- El **recibo** muestra **Cliente: Nombre** y la **vista previa del comprobante** también.

## Compatibilidad con Semana 9
El login es **opcional en la API**: sin `Authorization`, `POST /api/v1/orders` funciona igual (smoke 8/8, tareas A/B/C intactas). El JSON público del pedido no cambia; el nombre se consulta en `GET /api/v1/orders/{id}/customer` (solo el nombre, nunca el correo).

## Endpoints nuevos
| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/v1/auth/config` | Client ID público para el botón |
| GET | `/api/v1/auth/me` | Valida el token y devuelve nombre/correo |
| GET | `/api/v1/orders/{id}/customer` | Nombre del cliente del pedido |

## Configuración
1. Google Cloud Console → *APIs y servicios* → *Credenciales* → **ID de cliente de OAuth** → tipo **Aplicación web**.
2. **Orígenes autorizados de JavaScript**: `http://localhost`, `http://localhost:8019` (y la URL `https://….trycloudflare.com` si se comparte por túnel).
3. En `.env`: `GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com` (es público, no es un secreto).
4. `docker compose up -d --build --force-recreate`

## Seguridad
- CSP solo permite `https://accounts.google.com/gsi/*`. `Referrer-Policy: strict-origin-when-cross-origin` (requerido por Google).
- El token vive en `sessionStorage` de la pestaña (expira en 1 h) y nunca se guarda en el servidor.
