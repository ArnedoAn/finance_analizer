# Finance Analyzer v3

Servicio single-user para Colombia: correo IMAP, notificaciones/SMS de Message Mirror y registros manuales convergen en Firefly III. COP y `America/Bogota`; `main` es la única rama de desarrollo/despliegue. Firefly es el ledger. Lo dudoso se conserva en revisión, nunca se convierte silenciosamente en gasto. No hay sesiones, cookies, multi-tenant ni OAuth Google/Microsoft.

## Inicio local

Requiere Python 3.12+, [uv](https://docs.astral.sh/uv/), Firefly III y cuentas existentes.

```bash
cp .env.example .env
# Completar secretos, OWN_ACCOUNTS y MAIL_ACCOUNTS antes de arrancar.
uv python install 3.12
uv sync --frozen --extra dev
uv run --frozen alembic upgrade head
uv run --frozen uvicorn app.main:app --reload
```

`FINANCE_API_KEY` (≥32 caracteres) es obligatoria. `PHONE_API_KEY` es opcional y transitoria (≥32 caracteres y distinta de la clave admin): su primer uso válido la migra al dispositivo de ingesta `phone-legacy`; después se retira revocando ese dispositivo y quitando la variable. Genere secretos aleatorios independientes (por ejemplo, `openssl rand -hex 32`), nunca use los placeholders de los ejemplos. Configure `FIREFLY_BASE_URL`, `FIREFLY_API_TOKEN`, las cuentas propias y LLM. `uv.lock` fija dependencias de desarrollo y producción; actualícelo deliberadamente con `uv lock` cuando cambie `pyproject.toml`. No publique `.env`, cuerpos financieros, DB ni tokens.

## Configuración

Ejemplos completos: [.env.example](.env.example) y [env.production.example](env.production.example). Nombres en mayúsculas de los campos de `app/core/config.py`.

| Variable | Uso / valor predeterminado |
|---|---|
| `FINANCE_API_KEY` | Secreto administrativo obligatorio, ≥32 caracteres; header `X-API-Key`; no guardarlo en el teléfono |
| `PHONE_API_KEY` | Opcional y transitoria, ≥32 caracteres; se migra al dispositivo `phone-legacy` (solo ingesta `/notifications/webhook` y prueba `/auth/check`) |
| `APP_ENV`, `EXPOSE_DOCS` | development, false; en production OpenAPI y `/docs` deshabilitados salvo opt-in |
| `DATABASE_URL`, `DATABASE_URL_FILE` | `sqlite+aiosqlite:///./data/finance_v3.db`; PostgreSQL usa `postgresql+asyncpg://`; `_FILE` evita exponer la contraseña en el entorno |
| `TIMEZONE` | America/Bogota |
| `FIREFLY_BASE_URL`, `FIREFLY_API_TOKEN` | Ledger y personal access token |
| `OWN_ACCOUNTS` | Semilla inicial: lista JSON de cuentas propias (fuentes, aliases, últimos cuatro dígitos). Se importa una sola vez si la tabla `own_accounts` está vacía; después se administran con `/api/v1/own-accounts` y el archivo ya no se lee |
| `MANUAL_DEFAULT_ACCOUNT` | Efectivo; solo registro manual, nunca fallback de fuentes bancarias |
| `FIREFLY_CACHE_TTL_MINUTES` | 360 |
| `MAIL_ACCOUNTS` | Lista JSON, `[]` desactiva correo |
| `EMAIL_LOOKBACK_DAYS`, `EMAIL_MAX_BODY_CHARS` | 7, 12000 |
| `LLM_BASE_URL` | https://opencode.ai/inference/go/openai/v1 |
| `LLM_API_KEY`, `LLM_MODELS` | deepseek-v4.1-flash,hy3,mimo-v2.6-flash en orden |
| `LLM_USER_AGENT` | finance-analyzer/3.0 |
| `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL` | https://api.deepseek.com/v1, secreto opcional, deepseek-chat |
| `DEDUP_WINDOW_MINUTES`, `TRANSFER_PAIR_WINDOW_MINUTES` | 60, 30 |
| `MAX_AMOUNT_COP`, `REVIEW_AMOUNT_COP` | 50000000, 2000000; `REVIEW_AMOUNT_COP` es además el tope por defecto de la política de aprobación automática (la política vive en la base, no en el entorno; ver «Aprobación automática») |
| `INGEST_MAX_EVENTS_PER_HOUR` | 120; límite de eventos de ingesta por hora, contador por proceso (válido con un solo worker) |
| `FAIL_AFTER_HOURS` | 72 horas para fallos transitorios automáticos |
| `RAW_TEXT_RETENTION_DAYS` | 30 para todos los estados; el texto crudo se guarda cifrado (AES-GCM) mientras tanto |
| `SCHEDULER_ENABLED`, `CYCLE_CRON` | true, `0 * * * *` |
| `SENDER_LEARNING_CRON` | `0 3 1,15 * *` |
| `TRM_BASE_URL`, `TRM_TIMEOUT_SECONDS` | `https://www.datos.gov.co/resource/32sa-8pi3.json` (SODA, TRM oficial), 10; el job `fx_trm` (08:00 Bogotá) la guarda en `fx_rates` |
| `RECONCILE_CRON` | `0 4 * * *`, conciliación diaria con Firefly |
| `FIELD_ENCRYPTION_KEY_FILE`, `BACKUP_STATUS_FILE` | Clave maestra de cifrado de campos (archivo montado) y JSON de estado del respaldo |
| `NOTIFY_MIN_INTERVAL_MINUTES`, `PHONE_SILENCE_HOURS` | 15, 24 |

### Cuentas propias y confianza

`OWN_ACCOUNTS` reemplaza por completo `ACCOUNT_MAP` y solo siembra la tabla `own_accounts` en el primer arranque (se audita como `own_account.seed`); desde entonces las cuentas y sus fuentes se editan desde la app (`/api/v1/own-accounts`) y los cambios aplican sin reiniciar. Ejemplo del archivo semilla:

```json
[{"firefly_account":"Nequi","bank":"nequi","kind":"wallet","last4":[],"aliases":["nequi"],"sources":["app:com.nequi.MobileApp","sms:85954","email:nequi.com.co"]}]
```

`kind`: savings, checking, credit, wallet, pocket o cash. Configure los paquetes app, códigos SMS y dominios reales; no copie fuentes que no pertenezcan a su banco. Las cuentas deben existir en Firefly. Una fuente reconocida sin cuenta resoluble queda `needs_review`; no se contabiliza contra Efectivo. Contrapartes propias se registran como transferencias asset→asset o asset→liability, no ingreso+gasto. Las cuentas faltantes degradan salud y generan alerta.

IMAP usa usuario/contraseña de aplicación (`gmail`, `yahoo`, `icloud`, `custom`). Outlook: reenvío a IMAP, sin OAuth. Cada nombre de cuenta es estable y único. Custom requiere `host`; configure `authserv_ids` con los authserv-id reales del proveedor, no con dominios tomados de un correo. Lista vacía no confía en autenticación. Se valida el Authentication-Results confiable más alto y DKIM/DMARC alineado al dominio From parseado; no se confía en display names ni substrings. Una excepción `require_auth=false` es un opt-out explícito por regla y debe ser excepcional.

Se leen primero cabeceras con BODY.PEEK y cuerpo solo si corresponde. Se persisten UIDVALIDITY, UID, errores y fallos consecutivos; un mensaje roto no bloquea los siguientes. Aprender remitentes solo propone reglas `pending_approval`; aprobarlas exige revisión humana. Tras la migración v2 todas las reglas antiguas quedan pendientes, incluso las semillas: revisarlas y aprobarlas antes de reactivar el scheduler.

**SMS no autentica al banco.** Un remitente alfanumérico/código corto puede ser suplantado; una coincidencia con las fuentes (`sources`) de una cuenta propia no es prueba criptográfica. Con la política de aprobación automática activa (valor por defecto), un SMS o una notificación de una fuente propia que pase las comprobaciones se registra sin revisión humana; el riesgo se acota con el tope por monto, la pausa por «No lo reconozco», el deshacer y la auditoría (ver `docs/FINANCE-SECURITY.md`).

### Aprobación automática

La política es `{enabled, max_amount_cop}`, se guarda en la tabla `runtime_flags` (clave `auto_approve_policy`) y se cambia desde la app con `GET/PUT /api/v1/settings/auto-approve` (`read` / `review`, el `PUT` se audita como `settings.auto_approve`). Sin fila guardada vale `enabled=true` y `max_amount_cop=REVIEW_AMOUNT_COP`. Sustituye a `LLM_AUTO_APPROVE_MAX_COP`, `SMS_AUTO_APPROVE_MAX_COP` y `NOTIFICATION_AUTO_APPROVE_MAX_COP`: esas variables ya no existen y, si el entorno las trae, se ignoran.

Con `enabled=true`, un elemento de una fuente confiable (remitente de correo o cuenta propia) se registra solo si tiene monto válido ≤ `max_amount_cop`, cuenta propia resuelta, COP, fecha coherente y, si se usó el LLM, `verify_llm` sin discrepancias. Quedan en revisión solo los motivos duros: `sender_auth_failed`, extracción `review`, sin monto, monto sobre el tope, moneda extranjera, fecha incoherente, pausa por «No lo reconozco», posible duplicado manual/automático, cuenta propia no resuelta (y, con `enabled=false`, todo). Un comercio sucio ya no es motivo: se normaliza y se recorta a 60 caracteres; sin ningún nombre utilizable queda en revisión (`merchant absent`).

Una transferencia (`kind=transfer`) sin contraparte propia se registra como gasto (`withdrawal`) o ingreso (`deposit`, si el texto dice que se recibió) cuyo comercio es el nombre de la contraparte; `_merchant` crea la cuenta de gasto/ingreso en Firefly o reutiliza la existente. El elemento guarda `original_kind=transfer` y la etiqueta `transferencia`. Sin ningún nombre de contraparte sigue en revisión («ambiguous transfer counterparty»).

`POST /api/v1/inbox/auto-resolve` (`review`) vuelve a decidir en segundo plano todos los `needs_review` de fuentes automáticas (no manuales, no `sender_auth_failed`, no los que el dueño ya tocó): el parser local se ejecuta de nuevo y una extracción LLM guardada (monto, comercio y tipo) no vuelve a enviarse al proveedor; si falta la extracción, se extrae. Responde 202 `{queued}`; una sola reevaluación a la vez (409 `auto_resolve_running`).

**Correo compacto.** Al llegar a un estado final (created, ignored, duplicate, rejected, voided) un correo reemplaza `raw_text` por un extracto de máximo 600 caracteres: el asunto más las líneas que mencionan monto, comercio, cuenta o fecha; si el LLM se usó en esa misma pasada, su campo `summary` (≤ 300 caracteres, en español) lo sustituye. Mientras esté pending o needs_review conserva el texto completo. SMS y notificaciones no cambian.

### Secretos por archivo

`FINANCE_API_KEY`, `PHONE_API_KEY`, `DATABASE_URL`, `FIREFLY_API_TOKEN`, `LLM_API_KEY`, `LLM_FALLBACK_API_KEY`, `MAIL_ACCOUNTS`, `OWN_ACCOUNTS` y `FIELD_ENCRYPTION_KEY` soportan `<VAR>_FILE`. Monte archivos read-only, elimine el valor directo correspondiente y configure la ruta **dentro** del contenedor (por ejemplo, `PHONE_API_KEY_FILE=/run/secrets/phone_api_key`, `DATABASE_URL_FILE=/run/secrets/database_url`). Los archivos de cuentas contienen JSON; el de DB contiene la URL completa. Use Compose secrets o mounts en un override local de despliegue; no incluya secretos en la imagen, Git ni argumentos de build. El ejemplo Compose lee `.env`; las rutas del host no son rutas del contenedor.

## API `/api/v1`

Todas las rutas requieren `X-API-Key` salvo `/health/live`, `/health/ready` y `POST /devices/enroll`. La app usa claves por dispositivo (alcances `ingest`, `read`, `review`; `admin` implica todo); una clave `ingest` solo permite `/notifications/webhook` y `/auth/check`. `PHONE_API_KEY` es opcional y transitoria (se adopta como dispositivo `phone-legacy`). Los consumidores administrativos usan `FINANCE_API_KEY`. Tras 10 fallos 401/min por IP se limita el tráfico con claves inválidas; una clave válida no debe quedar bloqueada por esos fallos. No envíe headers de usuario/sesión. OpenAPI solo se expone en producción con `EXPOSE_DOCS=true`.

| Método | Ruta | Función |
|---|---|---|
| GET | `/auth/check` | `{ok:true}`, probar conexión sin efectos |
| GET | `/health/live`, `/health/ready`, `/health` | Probes públicos live/ready; diagnóstico autenticado |
| POST | `/notifications/webhook` | Persistir evento; procesar en background |
| GET | `/alerts?after_id=&limit=&include_acked=` | Avisos para la app (`read`): `{items,last_id}` en orden ascendente por `id` |
| POST | `/alerts/{id}/ack` | Marca el aviso como visto (`read`) |
| POST | `/transactions/manual` | Registro idempotente con `client_ref` obligatorio |
| GET | `/inbox?status=&source=&method=&limit=&offset=` | `{items,total}`, sin cuerpo |
| GET | `/inbox/{id}` | Detalle sin texto crudo por defecto; `?include_raw=true` es opt-in explícito para raw_text |
| POST | `/inbox/{id}/approve`, `/inbox/{id}/reject` | Resolver revisión; approve acepta correcciones |
| PATCH | `/inbox/{id}` | Corregir creado y actualizar Firefly |
| POST | `/inbox/{id}/void` | Eliminar en Firefly y marcar voided |
| POST | `/inbox/{id}/reprocess`, `/inbox/retry-failed` | Reencolar para nueva evaluación |
| GET/PUT | `/settings/auto-approve` | Política de aprobación automática (`read` / `review`) |
| POST | `/inbox/auto-resolve` | Reevaluar en segundo plano la revisión con la política actual (`review`, 202 `{queued}`) |
| GET | `/mail/accounts` | Configuración sin secretos, cursor y fallos consecutivos |
| POST | `/mail/accounts/{name}/test` | Prueba IMAP con error enmascarado |
| POST | `/mail/accounts/{name}/repoll?since=YYYY-MM-DD` | Releer rango histórico deliberadamente |
| GET/POST | `/senders` | Consultar/agregar reglas |
| POST | `/senders/{id}/approve` | Activar propuesta revisada |
| DELETE | `/senders/{id}` | Eliminar regla |
| GET | `/reports/summary?period=day\|week\|month&date=YYYY-MM-DD` | Totales por kind/categoría/cuenta y comercios |
| GET | `/processing/statistics` | Counts por estado/fuente/método, antigüedad pendiente y último ciclo real |
| POST | `/processing/run`, `/reconcile/run` | Ejecutar ciclo o conciliación Firefly |
| POST/GET | `/maintenance/reset`, `/maintenance/reset/{id}` | Solo `admin`: borrar todo lo de movimientos (Firefly y base) para reimportar; prueba en seco por defecto, confirmación `BORRAR MOVIMIENTOS`, tras un respaldo (`docs/contract-additions/ServerReset.md`) |
| POST | `/sync/all` | Refrescar caché Firefly |
| GET | `/sync/accounts`, `/sync/categories`, `/scheduler/status` | Consultar caché/jobs |
| POST | `/categories` | Crear en Firefly una categoría elegida explícitamente (`review`): `201` creada, `200` si ya existe (sin tildes ni mayúsculas); `docs/contract-additions/CategoryCreate.md` |
| POST | `/categories/defaults` | Crear en Firefly las categorías sugeridas que falten (`review`, idempotente): `{created, existing}`; mismo documento |
| GET | `/rules` | Reglas activas `{items,total}`; cada una trae `tags` (lista, `[]` si no tiene) |
| POST | `/rules` | Crear o actualizar una regla (`review`): `{match, pattern, category, merchant?, tags?}`; las etiquetas se suman a las del movimiento cuando la regla casa (`docs/contract-additions/AssistantActions.md`) |
| POST | `/assistant/batches/{id}/proposals` | Proponer un cambio o acción (`read`, `201`) |
| POST | `/assistant/batches/{id}/proposals/bulk` | 1 a 50 propuestas en una llamada (`read`): `{items:[{ok,proposal}\|{ok:false,error:{detail,code}}],created,failed}` en el mismo orden; un elemento inválido no tumba a los demás; lote cerrado `409`, inexistente `404` |
| POST | `/assistant/batches/{id}/apply` | Aprobar propuestas (`review`). Valida como siempre (`422`, `409`, `risky_requires_single`) y responde **`202 {run, batch}`** al instante; aplica en segundo plano, por fases y en serie. Misma `Idempotency-Key` en el mismo lote: devuelve ese run (`200`) sin aplicar de nuevo; otra clave con un run activo: `409 apply_running` con el run |
| GET | `/assistant/batches/{id}` | Lote con sus propuestas y `run` (el del último apply, `null` si no hay): `{id, status: running\|done\|failed, approved_ids, applied, failed:[{id,detail}], pending, started_at, finished_at}`; la app lo consulta cada 1,5 s mientras `running` (`docs/contract-additions/AssistantActions.md`) |
| POST | `/assistant/batches/{id}/revert` | Deshacer lo aplicado (`review`); `409 apply_running` mientras un apply sigue en curso |
| GET | `/debts` | Deudas activas con el saldo leído de Firefly: `{items,total,totals:{balance,installments_monthly}}` (`read`; `docs/contract-additions/DebtsIncomes.md`) |
| POST | `/debts` | Crear deuda (`review`, `201`): sin `firefly_account_id` crea el pasivo en Firefly y la cuenta propia (`loan`/`credit`); con él, lo vincula |
| PUT/DELETE | `/debts/{id}` | Editar (una tasa nueva se envía al pasivo) / desactivar sin borrar el pasivo (`review`) |
| GET | `/debts/{id}/schedule?extra_monthly=&extra_once=&extra_once_month=&mode=term\|installment` | Tabla de amortización desde el saldo, con seguro y abonos, contra la base sin abonos (`read`) |
| POST | `/debts/plan` | Avalancha y bola de nieve con efecto bola: `{avalanche?,snowball?}` (`read`, solo cálculo) |
| GET/POST | `/incomes` | Ingresos `{items}` (`read`) / crear (`review`, `201`) |
| PUT/DELETE | `/incomes/{id}` | Editar / desactivar un ingreso (`review`) |
| GET/PUT | `/settings/extra-income` | Umbral de ingreso extra en COP, por defecto 200.000 (`read` / `review`) |
| GET | `/fx/trm?date=YYYY-MM-DD` | TRM oficial del día, con caché; sin red usa la última conocida y `stale: true` (`read`) |
| GET | `/plan/month?month=YYYY-MM` | Ingresos esperados y recibidos, presupuesto, cuotas, disponible para abonar y sugerencia de reparto (`read`) |

### Message Mirror

URL directa: `https://financeapi.esclab.tech/api/v1/notifications/webhook`. No use un dominio que redirija POST. Probar conexión usa `GET /api/v1/auth/check`.

```bash
curl -X POST http://localhost:8000/api/v1/notifications/webhook \
  -H "X-API-Key: $PHONE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"event_id":"f044850a-4678-4dfd-9c4d-22c73a3a7167","type":"notification","app":"com.nequi.MobileApp","sender":"Nequi","title":"Compra","message":"Pagaste $25.000 en TIENDA","notification_key":"bank-event-123","post_time":1794877200000}'
```

El UUID `event_id` es estable en todos los reintentos del outbox nativo; `notification_key` es sbn.key y `post_time` epoch ms. Se conservan campos v2 (`timestamp`, `date`, `big_text`, etc.). Sin event_id se usa huella con timestamp; sin ambos se genera UUID, nunca se deduplica solo por contenido. Fechas fuera de 2020…mañana van a revisión con hora de recepción.

- 202: `{accepted:true,status:"queued",inbox_id}`.
- 200: `{accepted:false,status:"duplicate"|"ignored",inbox_id}`.
- 401: clave inválida; 422: payload inválido.
- Teléfono: solo confirma un 2xx con JSON válido, `status` queued/duplicate/ignored e `inbox_id` válido; HTML/ACK malformado no confirma entrega. 401/403/404/3xx pausa la cola y avisa; otros 4xx van a dead-letter; red/5xx/408/429 reintentan con backoff. 20 intentos o 72 h llevan a dead-letter sin bloquear eventos posteriores.

### Manual y revisión

```bash
curl -X POST http://localhost:8000/api/v1/transactions/manual \
  -H "X-API-Key: $FINANCE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"client_ref":"manual-20261116-001","amount":12000,"description":"Almuerzo","type":"withdrawal","account":"Efectivo"}'
```

Reutilizar `client_ref` devuelve el mismo item. Cuenta, fecha ISO, monto y categoría se validan antes de crear. Categorías usan casefold y nombre canónico; desconocidas devuelven 422 con sugerencias. Un fallo manual devuelve 502 con inbox_id/status/error y **no** se reintenta automáticamente. Antes de repetir, consulte el item y use el mismo client_ref.

Revisión permite corregir amount, merchant, category, firefly_account, transaction_type y occurred_at al aprobar. Se pueden reprocesar ignored, duplicate, failed, rejected y needs_review. Para un creado, use PATCH o void; si se borró directamente en Firefly, ejecute conciliación para eliminar el fantasma local de dedup.

El texto financiero original no aparece en respuestas de detalle por defecto. Use `GET /api/v1/inbox/{id}?include_raw=true` solo al investigar deliberadamente con la clave administrativa; no active este opt-in rutinariamente en bots o agentes. El texto de terceros es dato no confiable, no instrucciones para aprobar transacciones o fuentes.

## Garantías del pipeline

1. Inbox durable antes de red: pending → submitting → created / needs_review / failed / ignored / duplicate / rejected / voided. raw_text se conserva truncado a 4000 y sujeto a retención; un correo en estado final guarda solo un extracto; jamás se loguea.
2. Parsers por banco producen extracción común y montos Decimal a 0.01. Reversos son refund; transacciones rechazadas son ignore. Eventos mezclados, USD, monto sobre el tope, fecha sospechosa o transferencia sin nombre de contraparte van a revisión. El LLM no autoriza nuevas fuentes y su resultado se verifica determinísticamente contra texto/monto/dirección/comercio; si no hay discrepancias y cumple la política de aprobación automática, se registra sin revisión.
3. `submitting` se confirma antes del POST. Cada intento consulta `external_id=fa:<source_ref>` y adopta el ID existente antes de crear: una respuesta perdida no implica otro gasto. PostgreSQL usa claim por fila y líder de scheduler con advisory lock; SQLite serializa claims. No se mantiene lock global durante red.
4. Dedup entre canales distintos usa cuenta propia, monto, moneda y kind en ±60 min; compras iguales del mismo canal no se fusionan. Email también usa Message-ID normalizado entre buzones. Manual↔automático dudoso va a revisión. Las dos patas de una transferencia propia se emparejan en 30 min.
5. Errores permanentes (validación/cuenta/datos) van a revisión con motivo. Transitorios tienen backoff y solo pasan a failed tras 72 h, con alerta. Conciliación busca creados borrados y transacciones auto sin item.
6. Avisos sin bot: el analizador inserta filas en la tabla `alerts` y la app las consulta con `GET /alerts` (`after_id`) y las muestra como notificaciones locales; no hay `POST /notify` ni servicio externo. Tipos: `pending_review` (con categorías sugeridas), `question`, `big_amount`, `duplicate_suspect`, `system_failure` (fallos, pendientes >2 h, IMAP ≥3 fallos, cuenta inexistente, conciliación), `phone_silence`, `capture_gap`, `digest_daily`, `digest_weekly`, `month_close`, `budget_80`, `budget_100`, `key_expiring`, `backup_failed`, más los de anomalías y suscripciones. `NOTIFY_MIN_INTERVAL_MINUTES` es el anti-ruido por tipo/clave de deduplicación.

7. Resumen y vigilancia: el resumen diario (por defecto 20:00 Bogotá) y el semanal (domingos) se envían según la preferencia `digest_policy` (`GET/PUT /settings/digest`: `daily`, `weekly`, `hour`, `silence_days`); un job horario (`digest_tick`) decide qué toca y cada periodo se emite una sola vez. Con el diario activo, los `new_charge` de cobros registrados automáticamente por debajo del tope de aprobación automática no se emiten uno a uno: el resumen del día los menciona. Tras cada ciclo se vigilan las fuentes de cada cuenta propia, los buzones con fallos y el LLM: si algo que solía llegar (≥ 3 eventos en los 30 días previos) lleva `silence_days` días (3 por defecto) sin traer nada, se emite `capture_gap`.
8. Resumen instantáneo: `dashboard_snapshots` guarda por mes (`YYYY-MM`) lo que la app armaba contra Firefly (`GET /dashboard/snapshot?month=`, `POST /dashboard/snapshot/refresh?month=`). Se recalcula al final de cada ciclo (mes actual), un minuto después del último cambio de un ítem (`created`, corrección o anulación) y una vez al día para el mes anterior; una sola ejecución por mes a la vez. Pasadas 2 h se sirve con `stale=true` mientras se recalcula.
9. Errores del servidor: los últimos 300 (30 días como máximo) de ciclo, buzón, LLM, Firefly, respaldo y rutas se guardan en `server_errors`, redactados (sin claves, tokens, contraseñas ni texto de mensajes; cuerpo de la respuesta externa recortado a 4 KB) y se leen con `GET /system/errors` (alcance `review`). `DIGEST_CRON` y `WEEKLY_DIGEST_CRON` ya no existen: la hora la fija la preferencia del dueño.

10. Descripciones: un correo cuyo asunto es genérico («Alertas y Notificaciones») ya no se usa como descripción. El analizador la deriva de la extracción (resumen del LLM, o «Compra en…», «Pago PSE a…», «Transferencia a…»), la guarda en `description` y es la que va a Firefly y se ve en la bandeja; `POST /maintenance/descriptions/fix` (`dry_run=true` para ver ejemplos) corrige los movimientos ya creados por el camino de edición auditado.

**Privacidad de los avisos:** los títulos y cuerpos incluyen montos y comercios pero nunca texto crudo, OTP ni credenciales; solo los lee un dispositivo con alcance `read`.

## Migraciones y despliegue

```bash
docker compose -f docker-compose.dev.yml up --build
# Producción: completar .env desde env.production.example y fijar FINANCE_IMAGE por SHA.
docker compose -f docker-compose.prod.yml up -d
```

Ambas imágenes ejecutan `alembic upgrade head` **antes** de uvicorn; fallo de migración impide servir. Producción usa un worker y UID/GID `999:999`; mounts de data/logs deben ser escribibles por ese usuario. La imagen instala dependencias desde uv.lock sin herramientas de desarrollo. PostgreSQL 16 no publica puerto; la API se publica en loopback, con proxy HTTPS delante. No lance simultáneamente dos migradores sobre la misma DB; detenga la instancia antigua antes del upgrade. Mantenga una réplica con scheduler activo.

Los Dockerfiles fijan `python:3.12-slim` por digest del índice OCI multiarch (`sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`) obtenido de la API Registry de Docker Hub; uv 0.9.11 también usa digest multiarch de GHCR (`sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568`). Ambos índices incluyen linux/amd64 y linux/arm64. Al actualizar, consulte de nuevo los metadatos autenticados del registry y cambie deliberadamente todos los stages; una etiqueta nueva no actualiza estos pins automáticamente.

### Upgrade de v2 existente

1. Detener el servicio y programadores; respaldar DB de Finance Analyzer **y Firefly**. Guardar configuración fuera de Git. Para PostgreSQL, `pg_dump` consistente; SQLite debe copiarse detenido incluyendo WAL si existe.
2. Mantener `DATABASE_URL` apuntando a la misma DB v2. El default local cambió a finance_v3.db: configure explícitamente `sqlite+aiosqlite:///./data/finance_v2.db` para conservar datos existentes. Compose producción conserva `finance_analyzer_v2` y `postgres-v2-data` deliberadamente: no renombre ni borre el volumen.
3. Cambiar ACCOUNT_MAP por OWN_ACCOUNTS y configurar secretos/notificaciones. Generar las claves administrativas y del teléfono independientes (≥32 caracteres), actualizar Message Mirror con `PHONE_API_KEY` y conservar `FINANCE_API_KEY` solo en consumidores administrativos. Mantener scheduler deshabilitado durante revisión inicial.
4. Ejecutar `uv run --frozen alembic upgrade head` (o iniciar la imagen, que ejecuta lo mismo). Baseline `20261116_01` crea una DB vacía o agrega columnas/tablas v3 a las tres tablas single-user v2 existentes; no borra inbox, IDs, cursores ni estados previos. No use `alembic stamp head`: omitiría los cambios de esquema.
5. Revisar y aprobar reglas antiguas de remitentes; la migración las pone en pending_approval y exige autenticación. Comprobar salud/cuentas, pendientes, revisión y conciliación antes de habilitar scheduler. No fuerce repoll histórico sin necesidad.

La DB v1/multi-tenant **no** se migra en sitio: baseline rechaza tablas/esquema no reconocidos. Respalde v1, cree otra DB y conserve Firefly como ledger histórico; controle lookback para no reingestar. El downgrade de baseline no elimina datos: falla deliberadamente. Rollback exige detener v3, restaurar backup coherente de DB y configuración e imagen anterior; volver solo a la imagen v2 no deshace el esquema ni reglas de confianza.

GHCR ejecuta matriz SQLite/PostgreSQL 16, migración y tests antes del build, con ambas claves de prueba válidas. Todas las acciones están fijadas a commits verificados mediante refs de la API de GitHub; cada job declara permisos mínimos (quality: contents read; publicación: contents read y packages write; deploy: ninguno). PR solo verifica; publicación y deploy exigen `refs/heads/main`, **también en workflow_dispatch**. Coolify **falla** si faltan `COOLIFY_URL` (variable del repositorio) o `COOLIFY_TOKEN` (secret), o si webhook devuelve error; no hay deploy omitido silenciosamente. Use endpoint HTTPS directo y versión de imagen por SHA. El workflow alternativo deploy.yml fue retirado.

## Desarrollo y comprobaciones

```bash
uv sync --frozen --extra dev
uv run --frozen ruff check app tests alembic
uv run --frozen pytest
# TEST_DATABASE_URL selecciona DB desechable para pruebas; nunca apuntar a producción.
```

Las pruebas no deben abrir cuentas reales ni `.env` del desarrollador. Integración Firefly requiere opt-in explícito `RUN_FIREFLY_INTEGRATION=1` y credenciales de prueba. Revise también la migración sobre una copia v2 con datos en SQLite y PostgreSQL antes de producción. [Decisión de arquitectura](docs/enfoques.md). `docs/omo-model-config.md` configura agentes de desarrollo, no el LLM del servicio.
