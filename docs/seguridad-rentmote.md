# Auditoría de seguridad — Rentmote

**Objetivo:** `https://customerapp.rentmote.com/user/tenant` (+ `https://backend.rentmote.com/graphql`)
**Fecha:** 2026-09-28
**Método:** Playwright con sesión real + análisis del bundle de producción + recon pasivo.
**Restricción acordada:** sin acciones permanentes en la UI y sin peticiones que dejen rastro sospechoso
en los logs. Por eso **el IDOR, la fuerza bruta de OTP y el rate limiting NO se han probado en vivo**;
se documentan como riesgo + plan de prueba manual.

---

## Resumen ejecutivo

La base está bastante bien construida: introspección GraphQL desactivada, batching desactivado,
prevención CSRF de Apollo activa, CORS restrictivo, cookie `HttpOnly`+`Secure`, IDs no enumerables (ULID)
y sin `eval`/`dangerouslySetInnerHTML` en el código de la app. TLS 1.3 y HSTS presentes.

Los puntos calientes son tres: **el reset de contraseña por OTP numérico**, **mutaciones que reciben el
`id` del recurso por parámetro en vez de derivarlo del token** (vector clásico de IDOR sobre datos
bancarios) y **el correo (SPF roto + sin DMARC)**, que es la base de cualquier ataque de phishing
contra tus usuarios.

| # | Hallazgo | Severidad |
|---|----------|-----------|
| 1 | `resetPassword(otpCode)` identifica al usuario solo por el código OTP | **Alta** (verificar) |
| 2 | Mutaciones con `id`/`*Id` del cliente (`updateTenant`, `updateNote`, `rejectBudget`…) | **Alta** (verificar) |
| 3 | SPF duplicado (`permerror`) + `~all` + sin DMARC | **Alta** |
| 4 | Cookie `SameSite=None` innecesaria | Media |
| 5 | JWT HS256 de 30 días, sin `jti` → irrevocable | Media |
| 6 | Sin CSP / `frame-ancestors` / `nosniff` / `Referrer-Policy` | Media |
| 7 | Errores GraphQL verbosos y guardias de auth inconsistentes | Media |
| 8 | URLs de descarga firmadas y `fileName` del cliente en subidas | Media |
| 9 | `window.location.replace(checkoutURL)` sin validar host | Baja-Media |
| 10 | Fingerprinting (`x-powered-by`, sugerencias de enums, sin límite de coste GraphQL) | Baja |
| 11 | PII (email + rol) en `localStorage`; HSTS sin `includeSubDomains`; sin `security.txt` | Baja |

---

## 1. Alta — Reset de contraseña por OTP numérico sin usuario

Del bundle (`/assets/index-C1xLh5mR.js`) salen las operaciones reales:

```graphql
mutation ResetPassword($otpCode: Int!, $newPassword: String!) {
  resetPassword(resetPasswordInput: {otpCode: $otpCode, newPassword: $newPassword}) { ... }
}
mutation ValidateOtp($code: Int!, $isForRegistration: Boolean) { ... }
```

Observaciones:
- El input **no lleva email ni id de usuario**: el código OTP *es* el identificador. Quien adivine el
  código puede fijar la contraseña nueva de esa cuenta → account takeover.
- El tipo es `Int!`, es decir un entero (típicamente 4-8 dígitos). El espacio de búsqueda es pequeño.
- No se observaron cabeceras de rate limiting (`x-ratelimit-*`, `retry-after`) en el endpoint.

**Plan de prueba manual (con cuenta de test):** pedir un reset para la cuenta de test, no usar el código,
y hacer ~50 intentos erróneos. Comprobar: (a) que bloquea tras N intentos, (b) que caduca (≤10 min),
(c) que se invalida al cambiar la contraseña, (d) que el código no es reutilizable.
**Mientras tanto:** limitar intentos por IP y por cuenta, código de 6+ dígitos con expiración corta,
y añadir el email al `resetPasswordInput` para que el código no identifique solo.

## 2. Alta — Mutaciones que confían en el `id` enviado por el cliente

```graphql
mutation UpdateTenant($id: String, $email: String, $bankAccountNumber: String, ...) {
  updateTenant(id: $id, updateTenantInput: {...}) { firstName lastName }
}
```

`updateTenant` recibe `id` y permite cambiar `email` y `bankAccountNumber` (IBAN). También toman IDs del
cliente: `updateNote(id)`, `updateIncident(id)`, `rejectBudget(budgetId)`,
`approveExceededIncidentExpense(incidentExpenseId)`, `getIncidentForTenant(incidentId)`,
`getMessagesByIncident(incidentId)`, `ownerIncident(id)`, `createIncidentExpense(...)`, etc.

Si el resolver no verifica propiedad/rol, un inquilino podría reescribir el IBAN o el email de otro
usuario (y con el email, encadenar con el flujo de OTP de arriba). Los IDs son ULID (26 chars, no
enumerables), pero **se devuelven en claro en las respuestas** (`tenant.id`, `contract.id`,
`user.id`…), así que un usuario con visibilidad parcial de datos ajenos (owner, supplier) sí tendría
IDs válidos de otras partes.

**Plan de prueba manual:** con dos cuentas de test, reenviar una petición legítima de `updateTenant`
sustituyendo el `id` por el de la otra cuenta (y lo mismo con `getIncidentForTenant`). Debe fallar con
un error de autorización, no con éxito silencioso.
**Mientras tanto:** derivar el sujeto del token (`ctx.user.sub`) en el servidor y usar el `id` solo como
comprobación de igualdad; o exigir comprobación `resource.ownerId === ctx.user.sub` en cada resolver.

## 3. Alta — El dominio de correo se puede suplantar

```
TXT rentmote.com  →  "v=spf1 include:mailgun.org ~all"
                     "v=spf1 include:_spf.google.com ~all"   ← dos registros SPF
DMARC _dmarc.rentmote.com → (no existe)
MX    rentmote.com  →  smtp.google.com
```

Dos registros SPF = *permerror* según RFC 7208: los receptores pueden ignorar el SPF por completo.
Además `~all` es *softfail* y no hay DMARC. Un atacante puede mandar correos que parezcan de
`rentmote.com` (p. ej. "tu contraseña caduca, entra aquí") con alta probabilidad de llegar a la bandeja.

**Arreglo:** un único registro SPF que incluya *todas* las fuentes
(`v=spf1 include:_spf.google.com include:mailgun.org -all`) y un DMARC inicial
(`v=DMARC1; p=none; rua=mailto:...; pct=100`), pasando luego a `p=reject` cuando revises los informes.

---

## 4. Media — Cookie de sesión con `SameSite=None`

```
Set-Cookie: Authentication  (JWT)
  domain   = backend.rentmote.com   ✔ host-only
  secure   = true                   ✔
  httpOnly = true                   ✔
  sameSite = None                   ✘
  expira   = 30 días
```

`customerapp.rentmote.com` y `backend.rentmote.com` son **same-site** (misma eTLD+1 `rentmote.com`),
así que `SameSite=Lax` seguiría enviando la cookie en las peticiones XHR de tu app y **no rompe nada**.
Con `None`, la cookie viaja en cualquier petición cross-site: hoy te salva la prevención CSRF de Apollo
(exige `content-type: application/json`, que dispara preflight, y el preflight solo devuelve
`access-control-allow-origin` para tu dominio), pero es una capa menos.

**Arreglo:** `SameSite=Lax` (o `Strict`). Además, como la cookie es de `backend.rentmote.com`, un XSS en
el frontend no puede leerla (httpOnly + host distinto) ✔ — buen diseño.

## 5. Media — JWT de 30 días e irrevocable

```json
header  { "alg": "HS256", "typ": "JWT" }
payload { "sub": "01KZNGN6P4XAS2HR4PWY1XNJRG", "iat": ..., "exp": ... }   // 30 días
```

Sin `jti` ni id de sesión: no puedes invalidar un token concreto en el logout ni al cambiar la
contraseña ni al detectar un robo, salvo que mantengas una denylist o gires el secreto (lo que mata a
todos). 30 días es mucho para una app con pagos y datos bancarios.

**Arreglo:** access token corto (15-60 min) + refresh token rotativo en cookie separada, o sesión
opaca en servidor con revocación; añadir `jti` y comprobarlo en el middleware.

## 6. Media — Cabeceras de seguridad ausentes en el frontend

`customerapp.rentmote.com` solo envía `strict-transport-security`. Faltan:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; ...
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(), microphone=(), geolocation=()
```

Impacto: **clickjacking** sobre una app que confirma pagos y cambia datos bancarios (hoy cualquier web
puede enmarcar tu login y superponer UI engañosa), ausencia de mitigación de XSS y fugas de referrer.
En Netlify basta un fichero `_headers`. La política de cookies que muestra la app ("Cookie notice")
también merece una revisión de cumplimiento (no he visto banner de consentimiento granular, solo un
aviso con "I understand").

## 7. Media — Errores GraphQL verbosos y guardias inconsistentes

Sin token, cada resolver responde distinto, lo que revela qué middleware usa cada uno:

| Mensaje | Significado |
|---|---|
| `Token with problems` → `INTERNAL_SERVER_ERROR` | token presente pero ilegible |
| `Authorization header or authentication cookie is required` | guardia explícita ✔ |
| `Cannot read properties of null (reading 'userId')` | **resolver sin guardia**: hace `ctx.user.userId` sobre `null` |

Los casos `unreadNotificationsCount` y `userNotifications` **no tienen guardia de autenticación** y
revientan con un null-deref que se filtra al cliente (`INTERNAL_SERVER_ERROR`). Aunque no hay fuga de
datos (no devuelven nada), es un error interno expuesto y una señal clara de que las guardias están
aplicadas a mano, resolver por resolver — que es exactamente lo que produce los fallos de autorización
del punto 2.

**Arreglo:** un único plugin/middleware global que exija `ctx.user` (y rol) por tipo/field, devolver
`UNAUTHENTICATED`/`FORBIDDEN`, y ofuscar el texto de error en producción.

## 8. Media — Subida y descarga de ficheros

```graphql
mutation GetFileUploadUrls($inputs: [GenerateFileUploadInput!]!) {
  getUploadUrls(inputs: $inputs) { uploadUrl id fileName fileStatusOption }
}
query GetIncidentForTenant(...) { ... fileUploads { urlToDownload fileTypeOption } }
```

El cliente envía `fileName` y recibe una URL de subida y URLs de descarga. Verificar en servidor:
extensión y MIME reales (no el `Content-Type` del cliente), límite de tamaño, y sobre todo que
`urlToDownload` **caduca** (presigned URL de S3/R2 con expiración corta). Si son URLs permanentes o de
bucket público, cualquier documento sensible (DNI, contratos, facturas) queda accesible a quien obtenga
la URL — y estas URLs acaban en historiales, logs y capturas.

## 9. Baja-Media — Redirect a `checkoutURL` sin validar

```js
case "StripePaymentDataDataOutput": window.location.replace(B.data.createPayment.checkoutURL)
```

Sale del servidor, pero no se valida el host. Si algún día `checkoutURL` se pudiera manipular (dato
almacenado, SSRF, compromiso parcial), tendrías un *open redirect* dentro del flujo de pago — el peor
sitio posible. **Arreglo:** comprobar `new URL(checkoutURL).hostname` contra una allowlist
(`checkout.stripe.com`, `*.stripe.com`) antes de navegar.

El flujo de **Redsys** está bien planteado: el formulario se construye con pares `name/value` generados
en el servidor (incluida la firma) y se hace POST a `sis.redsys.es`, de modo que tocar `Ds_Merchant_Amount`
en el cliente invalidaría la firma. **Verificar** que la firma es HMAC-SHA256 con la clave en formato REST
y que `Ds_Merchant_Order`/`Amount` se recalculan y validan en el servidor contra el recibo.

## 10. Baja — Fingerprinting y robustez

- `x-powered-by: Express`, `x-render-origin-server: Render`, `server: cloudflare` → infra enumerada.
  Usa `app.disable('x-powered-by')`.
- Los errores de validación sugieren valores del schema (`Value "OWNER" does not exist in
  ClientAppTypeOption enum. Did you mean the enum value "USER"?`) → permite ir reconstruyendo enums.
- No hay límite de coste/aliasing: 30 alias en una petición se ejecutan sin problema (el *batching* sí
  está desactivado ✔ y hay límite de profundidad de introspección ✔). Recomiendo
  `graphql-validation-complexity`/`graphql-cost-analysis` + límite de alias.
- El login no permite enumerar usuarios ✔ (mensaje genérico `INVALID_CREDENTIALS`).

## 11. Baja — Misceláneo

- `localStorage.userData` guarda `{"username": "<email>", "usertype": "TENANT"}` → PII en storage,
  amplía el impacto de cualquier XSS. Guárdalo solo en memoria o en cookie.
- HSTS sin `includeSubDomains` ni `preload`.
- El SPA devuelve `200` con el `index.html` para **cualquier** ruta, incluidas `/.env`, `/.git/config`,
  `/.well-known/security.txt` → no hay 404 real (ruido para el monitoring) y no existe `security.txt`
  para que un investigador te reporte vulnerabilidades.
- Sin source maps en producción ✔ (`.js.map` devuelve el fallback, no el mapa).

---

## Lo que está bien hecho

- Introspección GraphQL desactivada en producción, *operation batching* desactivado, prevención CSRF
  nativa de Apollo Server.
- CORS restrictivo: `access-control-allow-origin` solo se emite para
  `https://customerapp.rentmote.com` (con `Origin: evil.example` o `null` no se refleja) con
  `allow-credentials: true` correctamente acotado.
- Cookie de sesión `HttpOnly` + `Secure` + host-only en `backend.rentmote.com`: un XSS en el frontend
  no puede robarla.
- Identificadores ULID: no son enumerables, lo que frena la fuerza bruta de IDOR.
- Sin `eval`, `new Function`, `document.write` ni `dangerouslySetInnerHTML` en el código de la app.
- TLS 1.3 (AES-256-GCM), HSTS activo, certificados Let's Encrypt al día.

## Pruebas pendientes (requieren cuenta de test y dejan rastro)

1. **IDOR horizontal/vertical** — dos cuentas, intercambiar `id` en `updateTenant`,
   `getIncidentForTenant`, `getMessagesByIncident`, `createPayment(receiptId)`.
2. **Escalada vertical** — token de *tenant* llamando a `getOwnerForOwner`, `getPropertiesForOwner`,
   `ownerOccupationSettlements`, `getSupplierPropertiesWithTenants` y viceversa.
3. **Fuerza bruta de OTP** y rate limiting de `login`/`forgotPassword`/`resetPassword`.
4. **Revocación**: hacer logout y reutilizar el JWT capturado; cambiar la contraseña y reutilizarlo.
5. **Pagos**: reenviar `createPayment` con un `receiptId` ya pagado, con un `receiptId` ajeno y con
   `receiptId` nulo/inexistente (sin llegar a confirmar el pago).
6. **Uploads**: subir un fichero con extensión engañosa (`.svg`, `.html`, doble extensión) y comprobar
   el `Content-Type` con el que se sirve `urlToDownload` (riesgo de XSS almacenado).
