# Decisión: single-user v3 en main

**Fecha: 2026-11-16. Estado: vigente.**

Único usuario en Colombia, COP/America/Bogota y Firefly III como ledger. `main` es la única línea de desarrollo y despliegue; las ramas individual/multi-tenant, sesiones y OAuth quedaron obsoletas. Objetivo: ningún gasto perdido, duplicado ni mal registrado en silencio; lo dudoso se conserva en revisión y se notifica al dueño.

## Contrato operativo

- Inbox durable para email, notification, sms y manual, persistido antes de red. Estados de revisión/rechazo/anulación explícitos; cuerpos truncados con retención y nunca logueados.
- OWN_ACCOUNTS contiene fuentes, aliases y productos; no existe fallback bancario a Efectivo. Transferencias entre propias no inflan ingresos/gastos.
- Reglas de remitentes exactas, autenticación mail alineada y aprendizaje solo como propuesta. Descarga header-first; un correo roto no detiene la cuenta.
- Parsers por banco con Extraction común, dinero Decimal, fechas ancladas y verificación semántica del LLM. USD, mezcla de eventos o datos ambiguos requieren revisión.
- Idempotencia por source_ref/client_ref y adopción Firefly por external_id antes de cada POST. Claim de fila, líder PostgreSQL del scheduler y un worker de despliegue, sin lock global durante red.
- Dedup entre canales distintos, Message-ID entre buzones y emparejamiento de transferencias. Nunca colapsar compras repetidas del mismo canal por contenido.
- Revisión, edición/void y conciliación hacen recuperables errores y borrados externos. Errores permanentes requieren revisión; transitorios tienen backoff y alerta.
- Telegram es push/notificación al dueño y registro manual auxiliar, no el canal principal. Message Mirror usa outbox nativo durable, ACK, pausa por autenticación y dead-letter visible.

## Datos y despliegue

Alembic reemplaza create_all de v2. Baseline v3 crea DB vacía o actualiza en sitio el esquema single-user v2 sin perder inbox ni cursores; remitentes heredados quedan pendientes de aprobación. Conservar DB/volumen v2 y respaldar antes de arrancar. V1/multi-tenant requiere DB separada y no se acepta como upgrade en sitio. Downgrade exige restaurar respaldo coherente, no borrar tablas.

Imágenes con uv.lock, sin root, migración previa y un worker. GHCR verifica SQLite/PostgreSQL antes de publicar; Coolify falla ante configuración incompleta. API key requerida, secretos por *_FILE, docs de producción opt-in. Ver README para comandos, variables, recuperación y rollback.
