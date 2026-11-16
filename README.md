# Finance Analyzer v2

Servicio **single-user** para Colombia: correo IMAP, notificaciones/SMS de Message Mirror y movimientos manuales convergen en Firefly III. Moneda COP; zona `America/Bogota`. `main` es la rama única de desarrollo/despliegue. No hay usuarios, sesiones, cookies, autenticación Telegram ni OAuth de Google/Microsoft.

## Inicio local

Requiere Python 3.12+, [uv](https://docs.astral.sh/uv/), una instancia Firefly III y sus cuentas asset existentes.

```bash
cp .env.example .env
uv python install 3.12
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --no-project uvicorn app.main:app --reload
```

Complete `FINANCE_API_KEY` (obligatoria), `FIREFLY_BASE_URL`, `FIREFLY_API_TOKEN`, las cuentas y las claves LLM en `.env`. Configure los nombres exactos de cuentas asset existentes; **no se crean cuentas asset**. `/docs` expone OpenAPI. Nunca publique `.env`, cuerpos financieros ni tokens.

## Configuración

Las variables completas están en [.env.example](.env.example); para producción use [env.production.example](env.production.example).

| Variable | Función / predeterminado |
|---|---|
| `FINANCE_API_KEY` | Secreto requerido; header `X-API-Key` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/finance_v2.db`; también PostgreSQL con asyncpg |
| `TIMEZONE` | `America/Bogota` |
| `FIREFLY_BASE_URL`, `FIREFLY_API_TOKEN` | Ledger y personal access token |
| `FIREFLY_DEFAULT_ASSET_ACCOUNT`, `MANUAL_DEFAULT_ACCOUNT` | `Efectivo` |
| `ACCOUNT_MAP` | JSON: paquete app, `sms:<remitente>` o substring del correo → cuenta asset existente |
| `FIREFLY_CACHE_TTL_MINUTES` | 360; se fuerza refresco si falta una cuenta |
| `AUTO_CREATE_CATEGORIES` | false; usar categorías existentes |
| `MAIL_ACCOUNTS` | JSON de N cuentas IMAP; `[]` desactiva correo |
| `EMAIL_LOOKBACK_DAYS`, `EMAIL_MAX_BODY_CHARS` | 7 y 12000 |
| `LLM_BASE_URL` | `https://opencode.ai/inference/go/openai/v1` |
| `LLM_API_KEY`, `LLM_MODELS` | OpenCode Go; `deepseek-v4.1-flash,hy3,mimo-v2.6-flash` en orden |
| `LLM_USER_AGENT` | `finance-analyzer/2.0` |
| `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL` | DeepSeek directo opcional; URL `https://api.deepseek.com/v1`, modelo `deepseek-chat` |
| `DEDUP_WINDOW_MINUTES`, `MAX_ATTEMPTS` | 60 y 8 |
| `SCHEDULER_ENABLED`, `CYCLE_CRON` | true, `0 * * * *` |
| `SENDER_LEARNING_CRON` | `0 3 1,15 * *` |

### Correo IMAP

```dotenv
MAIL_ACCOUNTS='[{"name":"personal","provider":"gmail","username":"owner@gmail.com","password":"app-password","folder":"INBOX","port":993,"ssl":true},{"name":"propio","provider":"custom","host":"imap.example.com","username":"owner@example.com","password":"secret"}]'
```

`provider` puede ser `gmail`, `yahoo`, `icloud` o `custom`; los tres primeros completan host. Use contraseñas de aplicación cuando el proveedor lo exija. Outlook se integra mediante reenvío automático a una cuenta IMAP, **no OAuth**. Cada nombre debe ser estable y único. IMAP usa stdlib en un thread, `EXAMINE` y `BODY.PEEK`, sin marcar como leído. Se persisten UIDVALIDITY, último UID, último poll y error por cuenta. El primer poll usa `SINCE` con el lookback. Se filtra From por remitentes conocidos; HTML se convierte a texto y se recorta.

## API `/api/v1`

Todas las rutas requieren `X-API-Key`, salvo `/health/live` y `/health/ready`. Clave ausente/incorrecta: 401. No envíe headers `X-Telegram-*`, `X-User-Id` o `X-Session-ID`.

| Método | Ruta | Uso |
|---|---|---|
| GET | `/health/live`, `/health/ready` | Probes públicos |
| GET | `/health` | Estado, versión y servicios database/firefly/llm/mail por cuenta |
| POST | `/notifications/webhook` | Ingesta durable y procesamiento inmediato en background |
| POST | `/transactions/manual` | Movimiento manual; 201 con inbox_id, firefly_transaction_id y status created |
| POST | `/processing/run?dry_run=false` | Poll IMAP y procesamiento inmediato; dry-run solo previsualiza pendientes existentes, sin persistir ni consultar IMAP |
| GET | `/processing/statistics` | by_status, by_source, by_method, pending, failed, last_run |
| GET | `/inbox?status=&source=&method=&limit=50` | Listado sin raw_text |
| GET | `/inbox/{id}` | Detalle con raw_text |
| POST | `/inbox/{id}/retry`, `/inbox/retry-failed` | Reencolar; `{requeued:n}` |
| GET | `/mail/accounts` | Configuración sin secretos y estado del poll |
| POST | `/mail/accounts/{name}/test` | Conectividad; `{ok,error}` |
| GET/POST | `/senders` | Consultar/agregar remitentes |
| DELETE | `/senders/{keyword}` | Eliminar remitente |
| POST | `/senders/seed`, `/senders/learn` | Semilla/aprendizaje |
| POST | `/sync/all` | Refrescar caché Firefly |
| GET | `/sync/accounts`, `/sync/categories` | Cuentas/categorías |
| GET | `/scheduler/status` | Estado de jobs |
| POST | `/scheduler/jobs/{job_id}/trigger` | Ejecutar `cycle` o `sender_learning` |

Se eliminaron `/auth/*`, `/emails/*`, `/processing/batch`, `/processing/jobs/*`, `/processing/single`, `/processing/dry-run`, `/processing/audit`, `/processing/retry-failed`, `/notifications/process`, `/notifications/history` y `/notifications/stats`. Los reintentos viven en inbox; no hay jobs HTTP para polling.

### Message Mirror

URL directa: `https://financeapi.esclab.tech/api/v1/notifications/webhook` (no el dominio antiguo que redirige POST). Configure la API key tanto para envíos Dart como nativos.

```bash
curl -X POST http://localhost:8000/api/v1/notifications/webhook \
  -H "X-API-Key: $FINANCE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"id":"phone-123","type":"notification","app":"com.todo1.mobile","sender":"Bancolombia","title":"Compra","message":"Compraste $25.000 en TIENDA","timestamp":"1794877200000","device":"phone"}'
```

Campos string: `id,type,app,sender,title,message,text,big_text,timestamp,date,category,device`; `type` es `notification` o `sms`. Extras (incluido `user_id`) se ignoran. Contenido: `big_text > message > text`; debe existir contenido. Fecha: timestamp epoch ms o ISO, luego `date` (`yyyy-MM-dd HH:mm`) local Bogotá.

- 202: `{accepted:true,status:"queued",inbox_id:n}`; se guarda antes de procesar.
- 200: `{accepted:false,status:"duplicate"|"ignored"}`. Fuentes no conocidas se guardan **sin cuerpo** por privacidad.
- 401: clave inválida. 422: payload inválido.
- Cliente: 2xx entregado; 4xx excepto 408/429 va a dead-letter persistente, visible y reencolable; red, 5xx, 408 y 429 se reintentan con backoff. Nunca descartar silenciosamente.

### Efectivo / manual

```bash
curl -X POST http://localhost:8000/api/v1/transactions/manual \
  -H "X-API-Key: $FINANCE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"amount":12000,"description":"Almuerzo","type":"withdrawal","account":"Efectivo"}'
```

`amount > 0`, descripción obligatoria, tipo `withdrawal` (default) o `deposit`; opcionales `category,account,merchant,date` (ISO). Bot Telegram: solo `ADMIN_ID`, `/gasto <monto> <descripción>` y `/ingreso`; `FINANCE_API_URL` + `FINANCE_API_KEY`, sin sesiones.

## Pipeline y garantías

1. Todas las fuentes pasan por `inbox_items`; se persiste antes de llamadas externas. Estados: pending, created, duplicate, ignored, failed. Detalle conserva resultado, método, intentos, próximo intento, error e ID Firefly.
2. Reglas deterministas descartan compras rechazadas, recordatorios de pago, extractos y OTP evaluando asunto y los primeros 300 caracteres compactados del cuerpo. Las palabras promocionales se evalúan solo en asunto, nunca en pies bancarios.
3. Parsers de plantilla regex (`app/parsers`) primero; LLM únicamente si no aplican. La moneda del parser procede del marcador junto al monto; el LLM devuelve COP o USD, sin inferir moneda de pies informativos. OpenAI-compatible JSON validado, tolera fences; error o JSON inválido pasa al siguiente modelo y luego al fallback opcional. Texto limitado a 4000 caracteres, cuentas asset y categorías válidas. Headers Bearer, User-Agent y `x-opencode-session` estable por ciclo.
4. Errores LLM/Firefly/red: backoff 5m, 15m, 1h, 3h, 6h, 12h…; al alcanzar 8 intentos queda failed, reintentable manualmente.
5. `source_ref` único: email `<account>:<uidvalidity>:<uid>`; teléfono SHA256(app|sender|content|timestamp). Dedup financiero **solo entre email y familia teléfono**, misma cuenta asset, monto exacto y ventana de 60 min. Nunca fusionar dos compras iguales del mismo canal.
6. Fecha/hora explícita del mensaje manda; si solo hay fecha y coincide con el día local, conservar occurred_at. Datetimes aware UTC internamente y offset Bogotá al enviar a Firefly; no utcnow naive.
7. Asset por ACCOUNT_MAP y luego default; nunca crear asset. Se crean expense/revenue de comercio con mayúsculas/espacios normalizados, sin prefijos DLO*, UBR*, PAYU*, MERCADOPAGO*; casar prefijos truncados de al menos 10 caracteres.
8. COP salvo USD/US$ explícito. Categorías existentes; `apply_rules=true` permite que manden reglas Firefly. Tags `auto` y canal; external_id `fa:<source_ref>`.
9. Caché compartida con TTL, no sync por item. Procesamiento serializado para evitar carreras de dedup. Un único worker/proceso con scheduler habilitado.

`dry_run=true` puede consumir una extracción LLM, pero devuelve su resultado en `previews`: method, amount, currency, merchant, category, asset_account, transaction_type, date y duplicate_of (además de inbox_id/status). No cambia inbox, estados IMAP, intentos ni Firefly. Las cuentas/categorías se pueden consultar para enriquecer el preview.

La imagen de producción usa UID/GID `999:999`; `/app/data` y `/app/logs` existen y pertenecen a ese usuario. Los bind mounts del host deben ser escribibles para `999:999`.

## Docker y despliegue

```bash
docker compose -f docker-compose.dev.yml up --build
# Producción: copiar env.production.example a .env, completar secretos y FINANCE_IMAGE
docker compose -f docker-compose.prod.yml up -d
```

Dev usa SQLite persistente en `finance-dev-data`, código app montado y hot reload; conecte su Firefly existente. Prod usa PostgreSQL 16 sin puerto público y volumen nuevo `postgres-v2-data`. La imagen Python 3.12 corre sin root, instala el wheel con código incluido y tiene healthcheck sin curl. API publicada solo en loopback: coloque proxy HTTPS delante. Para proxy en otra red Docker, conecte el servicio a esa red y use puerto interno 8000. Mantenga un solo worker para scheduler/dedup; no escale réplicas sin coordinación externa.

**Cutover v2:** no hay migraciones ni compatibilidad con tablas multi-tenant. Detenga v1, respalde su DB y credenciales fuera del repo, y cree una DB v2 vacía (`create_all` al iniciar). SQLite usa nombre nuevo y Compose un volumen PostgreSQL nuevo. No apunte v2 a la DB v1. Los movimientos ya creados siguen en Firefly; limite lookback/configure remitentes antes de activar el scheduler para evitar reingesta histórica. Reconfigure Mirror y bot con URL directa y API key. Gmail OAuth/token encryption y volúmenes de credenciales ya no se usan.

GHCR (`deploy-ghcr.yml`) ejecuta pytest y Ruff antes de construir/publicar; luego dispara Coolify si están configurados `COOLIFY_URL` y `COOLIFY_TOKEN`. Configure `FINANCE_IMAGE=ghcr.io/<owner>/finance-analyzer:<tag>`; preferir SHA para rollback. Haga backup de DB v2 y de Firefly; no loguee secretos ni cuerpos financieros.

## Desarrollo y comprobaciones

```bash
uv run --no-project ruff check app tests
uv run --no-project pytest
```

Fixtures: SQLite en memoria con Base v2, settings sin `.env`, key `test-key`, override get_db, sin lifespan/scheduler/red. Tests nuevos del pipeline/clientes terminan en `_v2.py`. El test Firefly en vivo se omite por defecto; requiere `RUN_FIREFLY_INTEGRATION=1`, `FIREFLY_BASE_URL` y `FIREFLY_API_TOKEN` explícitos y solo hace lecturas.

Véase [decisión de arquitectura](docs/enfoques.md). La guía `docs/omo-model-config.md` configura agentes de desarrollo, **no** los modelos LLM del servicio.
