# El auto-deploy se rompió con el rename de la cuenta de GitHub (2026-08-09)

## Síntoma

Ninguno. `webhook-mallo.service` figuraba `active`, `mallollm.alexanderoviedofadul.dev`
respondía **200**, el endpoint del webhook aceptaba las peticiones de GitHub y devolvía
**200 OK**… y **ningún push desplegaba**.

Lo único que quedaba era una línea en el log:

```
WARN  Webhook de repositorio no reconocido: alexanderoviedofadul/MALLO
```

## Causa raíz

El operador renombró su cuenta de GitHub de `bladealex9848` a `alexanderoviedofadul`. El
webhook validaba el repositorio contra una cadena literal:

```js
// webhook-mallo.js
if (!repository || repository.full_name !== 'bladealex9848/MALLO') {
    log('WARN', `Webhook de repositorio no reconocido: ${repository ? repository.full_name : 'unknown'}`);
    // ...y descarta el evento
}
```

Tras el rename, GitHub empezó a enviar `repository.full_name = "alexanderoviedofadul/MALLO"`.
La comparación fallaba y **todos los push se descartaban en silencio**.

Es el peor modo de fallo posible: el servicio hace exactamente lo que se le pidió —rechazar
lo que no reconoce—, responde 200 a GitHub para no reintentar, y desde fuera es
indistinguible de un deploy que sí ocurrió.

## Cómo se detectó

No revisando configuración, sino disparando un **ping real** desde la API de GitHub contra
los 57 webhooks del VPS. MALLO respondía 200, así que el ping por sí solo no bastó: lo que lo
delató fue **leer el log del servicio** tras el ping y encontrar el `WARN`.

De los 57 webhooks del VPS, éste fue **el único** con esta validación — y el único roto
directamente por el rename.

## Fix

Literal actualizado a `alexanderoviedofadul/MALLO` + `systemctl restart webhook-mallo`.
El script vive en el repo de configuraciones del VPS
(`Configuraciones_Equipos/VPS_Ubuntu_Hostinger/17-scripts-backend/webhook-mallo.js`), no en
este repositorio.

Verificado: ping real **200 OK** y `mallollm.alexanderoviedofadul.dev` → 200.

## Regla

**Un webhook no debe validar `repository.full_name` contra un literal `owner/repo`.**

Se rompe con cualquier renombrado de usuario u organización, y lo hace **sin fallar**: el
servicio sigue vivo, responde 200 y sólo deja una línea de log que nadie lee. Alternativas:

- comparar contra `repository.name` (`"MALLO"`), que **no** cambia al renombrar el owner;
- o leer el valor esperado de una variable de entorno, de forma que corregirlo no exija
  editar código.

Corolario: la firma HMAC ya garantiza que el evento viene de quien dice venir. Validar además
el nombre del repositorio aporta poco y añade este punto de rotura.
