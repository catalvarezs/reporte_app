# Deploy en Render

Esta app es un servidor Python (FastAPI) que genera PDFs con Chromium, así que
necesita un servicio con **proceso persistente** (no serverless). Render encaja
bien y tiene tier gratis.

## 1. Subir el repo a GitHub
Ya está en `origin/main`. Render lee el código desde ahí.

## 2. Crear el servicio en Render
1. Entrá a https://dashboard.render.com → **New + → Blueprint**.
2. Conectá este repositorio. Render detecta `render.yaml` y propone el servicio
   web `reporte-app` (Docker, plan free).
3. Antes de crear, te va a pedir las variables marcadas como secretas.

## 3. Variables de entorno (en el dashboard, NO en el repo)
`.env` está gitignoreado a propósito. En Render se cargan en
**Service → Environment**:

| Variable | Qué es |
|---|---|
| `SESSION_SECRET` | Clave para firmar la cookie de sesión. Generala con: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `CLIENT_ID` | client_id del cliente OAuth registrado para la URL de Render (ver paso 5) |
| `CLIENT_SECRET` | client_secret de ese cliente |
| `COOKIE_SECURE` | `true` (ya viene en render.yaml). Marca la cookie como Secure. |
| `NOTION_TOKEN` / `NOTION_DATABASE_ID` | Opcionales; solo si usás la integración con Notion. |

## 4. Primer deploy
Render construye la imagen y levanta el servicio. Te queda una URL tipo
`https://reporte-app.onrender.com`. El healthcheck pega a `/`.

## 5. Registrar el redirect_uri en Instance (IMPORTANTE)
El OAuth de Instance solo acepta callbacks **registrados**. El cliente que usamos
en local apunta a `http://127.0.0.1:8000/callback`; para la URL pública hay que
registrar uno nuevo con el callback de Render y usar ESAS credenciales.

Con la URL ya conocida, registrá un cliente nuevo (ajustá la URL):

```bash
python -c "
import httpx
r = httpx.post('https://backendia.instancelatam.com/sse/register', json={
  'client_name': 'Update Comercial (Render)',
  'redirect_uris': ['https://reporte-app.onrender.com/callback'],
  'grant_types': ['authorization_code'],
  'response_types': ['code'],
  'token_endpoint_auth_method': 'client_secret_post',
}, timeout=30)
print(r.status_code); print(r.json())
"
```

Copiá el `client_id` y `client_secret` que devuelve a las variables `CLIENT_ID` /
`CLIENT_SECRET` de Render y volvé a desplegar (o guardá las env vars, que dispara
un redeploy). Listo: el login va a funcionar contra la URL pública.

## Notas / límites
- **Tier free**: el servicio se duerme tras inactividad; el primer request luego
  de dormir tarda ~30–60 s (cold start). Para uso constante, plan pago.
- **No escalar a múltiples instancias**: el caché de reportes
  (`_REPORT_CACHE`) vive en memoria del proceso. Con 1 instancia (free) el botón
  de Descargar PDF/HTML funciona; con varias instancias podría fallar el lookup
  por `report_id`. Si algún día se escala, mover ese caché a almacenamiento
  externo (Redis, etc.).
- **Seguridad**: la app muestra ventas por cliente detrás del login OAuth. Si la
  exponés en internet, considerá acceso restringido adicional.
