# Enfoques: individual vs multi-usuario

**Estado:** decisión tomada 2026-09-13. Foco actual: **individual**.
**Nota:** este doc es la referencia persistente. El plan operativo vive en `.omo/plans/mejoras-postrefactor.md` (no versionado).

## Historia del código

| Enfoque | Commits | Rama |
|---|---|---|
| Individual (v1) | `390d5b7` … `f6558ba` | nueva: `individual` |
| Multi-usuario (v2) | `bd246d0` … `360c2f9` | `main` (desplegado) |

- `f6558ba` es el **último commit 100% single-user** (antes de `bd246d0`, que creó `app/core/session.py` y metió `session_id` en todo).
- `main` actual = multi-usuario; no se altera su estrategia de despliegue (GHCR → Coolify).

## Decisión

Dos ramas vivas:

- **`main`** = multi-usuario. Sigue siendo el producto desplegado.
- **`individual`** = nueva rama creada desde `f6558ba` + port selectivo de mejoras genéricas.
- El trabajo nuevo se hace primero en `individual`.

### Por qué ramas y no otra cosa

- Multi-tenant es **transversal**: 349 referencias a `session_id` en 22 archivos, `session.py`, columnas en 10 tablas, fan-out del scheduler, auth Telegram, override `user_id` en webhook. Un feature-flag runtime contaminaría cada constructor, repo y query.
- La v1 ya existe y funciona: partir de `f6558ba` es más barato que borrar 349 referencias desde HEAD.
- Repos separados duplican CI, deploy y dependencias. No conviene.

## Reglas de convivencia (para que ambos vivan)

**Compartido — va a las dos ramas:**
`app/clients/*`, servicios núcleo (`sync`, `transaction`, `notification` sin user-resolution), `models/schemas`, tests base, docker, workflows.

**Solo multi-usuario — NUNCA se porta a `individual`:**
`app/core/session.py`, `session_id` en modelos/repos, cadena de resolución en `dependencies.py`, `/auth/telegram/*`, estado PKCE multi-sesión, override `user_id` del webhook, `_discover_active_session_ids`, migración legacy de esquema.

**Flujo de cambios compartidos:** se escriben/comprueban en `individual` → se portan a `main` con cherry-pick selectivo. `individual` **nunca** absorbe `main` completo (arrastraría código de sesiones).

## Plan de trabajo

- **P0 (en `main`):** baseline git — `.gitattributes` (`* text=auto eol=lf`) + `git add --renormalize .` + commit de normalización + commit del refactor ponytail. Sin esto no hay diffs legibles ni ramas limpias.
- **P1:** `git branch individual f6558ba && git push -u origin individual`.
- **P2 (en `individual`):**
  - re-aplicar el refactor ponytail (clients, parsers, schemas, exceptions, config, logging, timezone, dedup gmail/sync/notification, tests);
  - fixes P1 del plan (kwargs `BatchProcessResponse`, `.env` stale, README);
  - fixture de integración autoprovisionada (contenedor Firefly efímero, patrón Testcontainers).
- **P3 (en `individual`, opcional):** cherry-pick `c8f0c43` (workflow GHCR), partes Gmail de PKCE (`3fed19a`), refinamiento de known senders (`838c45f`) si aplica sin arrastrar sesiones.
- **P4:** cuando `individual` esté estable, retomar `main`.

## Qué NO va a individual (YAGNI single-user)

- **Async processing jobs** (`ProcessingJob`, polling `/jobs/{id}`): un usuario, volumen bajo; `POST /processing/batch` síncrono basta. Reevaluar solo si los batches se vuelven lentos.
- Endpoints/multi: Telegram, `X-Telegram-*`, `X-User-Id`, cookies de sesión.
- `scheduler` fan-out multi-sesión: en individual hay una sola sesión; scheduler directo.

## Docker / tests de integración (aplica a ambas ramas)

- Docker está instalado, pero el socket no es accesible: `sudo usermod -aG docker $USER` + cerrar sesión/relogin (o usar `sudo docker ...`).
- La suite de integración se autoprovisiona: si hay Docker, levanta un `fireflyiii/core:latest` efímero (sqlite interno, sin volumen), registra el primer usuario por HTTP, crea un Personal Access Token y lo destruye al final. Sin Docker → `pytest.skip` en <2 s.
- Si `FIREFLY_BASE_URL`/`FIREFLY_API_TOKEN` ya apuntan a una instancia viva, se usa esa y no se levanta contenedor.

## Referencias

- Fragmentación por commits: `git diff f6558ba..360c2f9 --stat` (34 archivos, +2843/−313).
- Detalle del corte multi-usuario: `git show bd246d0 --stat`.
