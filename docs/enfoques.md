# Decisión: single-user v2 en main

**Fecha: 2026-11-16. Estado: vigente.**

`main` es ahora la única línea de desarrollo y despliegue de Finance Analyzer **single-user v2**, para el dueño. La estrategia anterior de conservar `main` multi-usuario y una rama `individual` separada queda **obsoleta**. No se debe volver a `individual` ni restaurar el diseño multi-tenant.

## Corte limpio

- Eliminar sesiones, session_id en DB/repositorios, cookies y headers Telegram/usuario/sesión, fan-out del scheduler y override user_id del webhook.
- Proteger API con FINANCE_API_KEY / X-API-Key; únicamente health/live y health/ready son públicos.
- Base v2 nueva con create_all, sin migraciones ni lectura de DB anterior. Respaldar v1 antes del corte; Firefly conserva el ledger.
- N cuentas IMAP con usuario/contraseña de aplicación. Gmail/Yahoo/iCloud/custom; Outlook vía reenvío. Sin OAuth Google/Microsoft ni cifrado local de tokens OAuth.
- Inbox durable único para email, notification, sms y manual. Reglas → parsers → LLM OpenCode Go con fallback opcional DeepSeek → Firefly.
- COP y America/Bogota; fechas aware, cuentas asset existentes, categorías existentes por defecto y reglas Firefly habilitadas.
- Dedup de source_ref y dedup cross-channel email/teléfono con monto/cuenta/ventana exactos; nunca fusionar compras repetidas del mismo canal.
- Un ciclo programado poll+procesamiento, aprendizaje de remitentes y procesamiento inmediato de webhook. Sin protocolo HTTP de jobs/polling.

## Clientes y despliegue

Message Mirror usa URL directa financeapi.esclab.tech, API key y cola persistente con dead-letter para errores permanentes. El bot solo permite finanzas a ADMIN_ID y usa la misma API key; no sesiones. GHCR → Coolify continúa sobre main, con tests y Ruff antes del build. Consultar README para variables, endpoints, operación y corte de DB.

## Historia (no operativa)

El repositorio tuvo una etapa individual v1 y luego una etapa multi-usuario; la decisión del 2026-09-13 propuso ramas vivas separadas. La decisión del **2026-11-16 reemplaza esa propuesta por completo**. Los módulos, endpoints y pruebas de sesión/OAuth anteriores no son referencias válidas para nuevas implementaciones.
