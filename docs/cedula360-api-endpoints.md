# API de cedula360 — Mapa completo de endpoints

> Referencia generada el 2026-06-23 para la integración MALLO ↔ cedula360.
> Base URL: `https://cedula360.tech/api` · Backend Express (Node.js) en `:3081`.

## Autenticación (4 modos)

| Modo | Cómo | Uso |
|---|---|---|
| **X-API-Key** | header `X-API-Key: c360_live_...` | Integraciones programáticas (lo que usa MALLO) |
| **JWT** | cookie httpOnly o `Authorization: Bearer` | Sesión web (frontend) |
| **Admin** | JWT con `role=admin` | Panel de administración |
| **Pública** | sin auth | Catálogos, salud, blog, planes |

Rate limits: `/api` 120/min · `/api/auth` 10/min · `/api/fuentes` 30/min · `/api/contenido/generar` 6/min · `/api/imagen` 12/min. Las consultas pesadas además consumen **cuota por plan**.

---

## 1. Consultas oficiales (`/api/fuentes/*`, `/api/consulta*`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET | `/api/fuentes/` | Catálogo de fuentes + estado captcha | Pública |
| GET | `/api/fuentes/tipo/:tipoDoc` | Fuentes por tipo (CC/NIT/PL…) | Pública |
| GET | `/api/fuentes/gating` | Tabla de fuentes pagas + plan mínimo | Pública |
| GET | `/api/fuentes/all/:cc?tipo=CC` | **Dossier consolidado** (multifuente) | API Key + cuota |
| GET | `/api/fuentes/datos-gov/:cc` | Datasets datos.gov.co | Pública |
| GET | `/api/fuentes/reps/:doc` | REPS (médicos/profesionales) | Pública |
| GET | `/api/fuentes/rues/:cc` · `/rues-co/:nit` | RUES (empresas) | Pública |
| GET | `/api/fuentes/rues-co/buscar/:query` | RUES por razón social | Pública |
| GET | `/api/fuentes/contraloria/:cc` | Antecedentes fiscales (PDF caché) | Pública |
| GET | `/api/fuentes/contraloria-fiscal/:cc` | Antecedentes fiscales EN VIVO (Playwright+captcha) | API Key + cuota |
| GET | `/api/fuentes/policia-judicial/:cc` | Antecedentes judiciales EN VIVO | API Key + cuota |
| GET | `/api/fuentes/registraduria/:cc` | Censo electoral | Pública |
| GET | `/api/fuentes/adres/:cc` | Afiliación EPS (BDUA) | Pública |
| GET | `/api/fuentes/rama-judicial/nombre/:nombre` | Procesos por nombre | Pública |
| GET | `/api/fuentes/rama-judicial/radicado/:radicado` | Proceso por radicado (23 díg.) | Pública |
| GET | `/api/fuentes/radicado/:rad` | Radicado consolidado (caché) | API Key + cuota |
| POST/GET | `/api/fuentes/radicado/:rad/analisis-ia` | Análisis IA del expediente | API Key + cuota |
| GET | `/api/fuentes/radicado/:rad/documento/:guid[/descargar]` | Documento del expediente / PDF | API Key |
| GET | `/api/fuentes/despacho/:codigo` | Ficha despacho judicial | API Key |
| GET | `/api/fuentes/dominio/:dominio` | WHOIS + DNS + scraping | API Key + cuota |
| GET | `/api/fuentes/telefono/:numero` | Celular CO (HLR + portabilidad) | API Key + cuota |
| GET | `/api/fuentes/ip/:ip` | Geolocalización IP (cascada) | API Key |
| GET | `/api/fuentes/sanciones/:nombre` | Sanciones locales (OFAC/UN/EU + PEP CO) | API Key + cuota |
| GET | `/api/fuentes/sanciones-premium/:nombre` | OpenSanctions API (Premium+) | API Key + cuota |
| GET | `/api/fuentes/listas-restrictivas/:query` | OpenSanctions ampliado | API Key |
| GET | `/api/fuentes/dian-rut/:doc` | RUT MUISCA (DIAN) | Pública |
| GET | `/api/fuentes/web-search` · `/web-fetch` | Búsqueda/fetch web (wrapper Ollama) | API Key |
| POST | `/api/fuentes/enriquecer-ia` | IA + web_search para complementar dossier | API Key |
| POST | `/api/consulta` · `/consulta/iniciar` · `/confirmar` | Consulta ADRES (con captcha) | JWT |
| GET | `/api/consultas` · `/consulta/:id` | Historial / detalle de consultas | JWT |

## 2. IA / RAG / Legal (`/api/ai`, `/api/rag`, `/api/legal`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET | `/api/ai/modelos` | Lista modelos IA disponibles | Pública |
| POST | `/api/ai/resumir/:cc` | Resumen ejecutivo de dossier | JWT |
| POST | `/api/ai/analizar-antecedentes/:cc` | Análisis SIRI de antecedentes | JWT |
| POST | `/api/rag/preguntar` | **Q&A sobre dossier cacheado** (RAG) | JWT |
| GET | `/api/legal/info` · `/health` | Capacidades del Asistente Legal | API Key (Business+) |
| POST | `/api/legal/ask` | **Pregunta al Asistente Legal** (RAG jurídico) | API Key (Business+) |
| GET | `/api/legal/buscar?q=` | Búsqueda en corpus jurídico | API Key (Business+) |
| GET | `/api/legal/fuentes/*` · `/leyes-api/*` | Passthrough a 13 fuentes jurídicas / leyes.co | API Key (Business+) |
| `/api/minimax/*` | Suite multimodal (chat/search/image/video/speech/music/vision) | API Key (Enterprise/Despachos) | (ver docs/minimax-api) |

## 3. Contenido IA (`/api/contenido`, `/api/imagen`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET | `/api/contenido/tipos` | Tipos + tope diario | API Key |
| POST | `/api/contenido/generar` | Genera artículo/boletín/investigación | API Key (Premium+) |
| GET | `/api/contenido/[:id][/pdf]` | Lista / detalle / PDF | API Key |
| GET | `/api/imagen/info` · `/usage` | Capacidades / uso PhotoClone | API Key |
| POST | `/api/imagen/decompose` · `/edit` · `/transform` | Capas RGBA / editar con prompt / resize | API Key (Premium+) |

## 4. Usuario / cuenta (`/api/usuario`, `/api/usuario/api-keys`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET/PATCH | `/api/usuario/me` | Perfil (plan, role, cuota) | API Key / JWT |
| GET | `/api/usuario/uso` · `/pagos` | Historial de consultas / pagos | API Key / JWT |
| GET/PUT/DELETE | `/api/usuario/smtp[/test]` | BYO-SMTP | API Key / JWT |
| GET/POST/DELETE | `/api/usuario/push[/:id][/test]` | Notificaciones push (ntfy) | API Key / JWT |
| GET/POST/DELETE | `/api/usuario/whatsapp[/start][/qr][/test]` | BYO-WhatsApp (Baileys) | API Key / JWT |
| GET/POST/DELETE | `/api/usuario/api-keys[/:id]` | Gestión de API keys (Premium+) | API Key / JWT |
| GET | `/api/usuario/api-keys/usage` | Estadísticas de uso de keys | API Key / JWT |
| POST | `/api/usuario/api-keys/test` | Consulta de prueba | API Key / JWT |

## 5. Alertas premium (`/api/alertas`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET/POST | `/api/alertas/` | Lista / crea alerta (CC/NIT/PL/RAD/TEL…) | API Key / JWT (Premium+) |
| PATCH/DELETE | `/api/alertas/:id` | Actualiza / elimina | API Key / JWT |
| GET | `/api/alertas/:id/historial` | Snapshots del dossier | API Key / JWT |

## 6. Auth y MFA (`/api/auth`, `/api/auth/mfa`)

- **Auth:** `login`, `logout`, `register`, `confirm-email`, `request-password-reset`, `reset-password`, `resend-confirmation`, `push-login`, `biometric-login`, `whatsapp-login`.
- **MFA:** `mfa/status`, `mfa/totp/{setup,confirm,disable}`, `mfa/otp/{request,verify,disable}`, `mfa/push/{topics,test,enable,disable}`, `mfa/backup-codes/regenerate`, `mfa/challenge`, `mfa/verify`, `mfa/magic-link/{request,consume}`. (Mayoría JWT; challenge/verify/magic-link públicos.)

## 7. Planes y pagos (`/api/planes`, `/api/wompi`)

| Método | Ruta | Propósito | Auth |
|---|---|---|---|
| GET | `/api/planes/` | Catálogo de planes | Pública |
| GET | `/api/wompi/productos` · `/status` | Productos / estado Wompi | Pública |
| POST | `/api/wompi/checkout[/integrity]` | Payment Link / datos firmados | JWT |
| POST | `/api/wompi/webhook` | Webhook de pago (firma verificada) | Pública (firma) |

## 8. Firmas, blog, i18n, marketing, admin, webhooks

- **Firmas:** `/api/admin/firmas/{health,config,templates,submissions}` (Admin), `/api/integraciones/firmas/*`, `/firmas/:slug` (pública), `/api/webhooks/docuseal` (HMAC).
- **Blog:** `/api/blog/{posts,tags,featured,posts/:slug[/comments]}` (pública) + `/api/blog/admin/*` (Admin, incl. `generate` con IA).
- **i18n:** `GET /api/i18n/langs`, `POST /api/i18n/translate` (TranslateGemma, pública).
- **Marketing/leads:** `/api/leads/newsletter` (pública), `/api/afiliados/{codigo,conversiones,comisiones}`, `/api/admin/{leads,mautic,meta-ads,google-ads,inbox}` (Admin).
- **Admin:** `/api/admin/settings`, `/api/admin/documentos` (CRM), `/api/usuarios` (interno).
- **Webhooks:** `/api/webhooks/{docuseal,mautic}`, `/api/sms/webhook/dlr`.

## 9. Salud / utilidades

`GET /api/health` · `/api/mcp/health` · `/api/public-config` · `/api/web-check/*` (Premium+) · `/api/parlaToolbar/*`.

---

**Relevantes para integrar en MALLO** (además de `fuentes` y `minimax` ya integrados):
`/api/ai/modelos` y `/api/ai/resumir`, `/api/rag/preguntar` (Q&A sobre dossiers), `/api/legal/ask` + `/api/legal/buscar` (asistente jurídico RAG), `/api/i18n/translate` (traducción local), `/api/contenido/generar`.
