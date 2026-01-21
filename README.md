# Finance Analyzer

> Microservicio de automatización financiera que procesa emails de Gmail, analiza transacciones con IA (DeepSeek), y crea registros automáticos en Firefly III.

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## 📋 Tabla de Contenidos

- [Características](#-características)
- [Arquitectura](#-arquitectura)
- [Requisitos](#-requisitos)
- [Instalación](#-instalación)
- [Configuración](#-configuración)
- [Uso](#-uso)
- [API Endpoints](#-api-endpoints)
- [Flujo de Procesamiento](#-flujo-de-procesamiento)
- [Desarrollo](#-desarrollo)
- [Troubleshooting](#-troubleshooting)

## ✨ Características

- **📧 Integración Gmail**: OAuth 2.0 con permisos read-only
- **🤖 Análisis con IA**: Extracción semántica de transacciones con DeepSeek
- **💰 Firefly III**: Sincronización automática de cuentas, categorías y transacciones
- **🔒 Seguridad**: Tokens cifrados, variables de entorno, sin hardcodeo
- **🔁 Idempotencia**: Un email = una transacción (sin duplicados)
- **📊 Auditoría**: Log completo de cada procesamiento
- **🧪 Modo Dry-Run**: Prueba sin crear transacciones reales
- **⏳ Reintentos**: Manejo automático de fallos temporales
- **🐳 Docker Ready**: Configuración lista para producción

## 🏗 Arquitectura

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Finance Analyzer                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────────┐  │
│  │  Gmail   │───▶│ DeepSeek │───▶│  Sync    │───▶│   Firefly III    │  │
│  │  Client  │    │    AI    │    │ Service  │    │     Client       │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────────────┘  │
│       │                │               │                   │            │
│       │                │               │                   │            │
│       ▼                ▼               ▼                   ▼            │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                    SQLite Database                                │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐   │  │
│  │  │ Processed   │  │  Audit      │  │  Account/Category Cache │   │  │
│  │  │   Emails    │  │   Logs      │  │                         │   │  │
│  │  └─────────────┘  └─────────────┘  └─────────────────────────┘   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Estructura del Proyecto

```
finance_analizer/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI application
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py           # Pydantic Settings
│   │   ├── exceptions.py       # Custom exceptions
│   │   ├── logging.py          # Structured logging
│   │   └── security.py         # Token encryption
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py          # Pydantic models
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLAlchemy async setup
│   │   ├── models.py           # ORM models
│   │   └── repositories.py     # Data access layer
│   ├── clients/
│   │   ├── __init__.py
│   │   ├── gmail.py            # Gmail API client
│   │   ├── deepseek.py         # DeepSeek AI client
│   │   └── firefly.py          # Firefly III client
│   ├── services/
│   │   ├── __init__.py
│   │   ├── email_processor.py  # Main orchestration
│   │   ├── sync_service.py     # Firefly sync
│   │   └── transaction_service.py
│   └── api/
│       ├── __init__.py
│       ├── dependencies.py     # FastAPI DI
│       ├── routes.py           # Router config
│       └── endpoints/
│           ├── auth.py
│           ├── emails.py
│           ├── health.py
│           ├── processing.py
│           └── sync.py
├── credentials/                 # OAuth credentials (gitignored)
├── data/                        # SQLite database (gitignored)
├── tests/
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## 📦 Requisitos

- Python 3.11+
- Firefly III (local o remoto)
- Cuenta de Google Cloud con Gmail API habilitada
- API Key de DeepSeek

## 🚀 Instalación

### 1. Clonar repositorio

```bash
git clone <repository-url>
cd finance_analizer
```

### 2. Crear entorno virtual

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -e ".[dev]"
```

### 4. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus valores
```

## ⚙️ Configuración

### Google Cloud / Gmail API

1. Ir a [Google Cloud Console](https://console.cloud.google.com/)
2. Crear proyecto o seleccionar existente
3. Habilitar **Gmail API**
4. Crear credenciales **OAuth 2.0 Client ID** (tipo: Desktop App)
5. Descargar JSON y guardar como `credentials/google_credentials.json`

### DeepSeek API

1. Obtener API key en [DeepSeek](https://platform.deepseek.com/)
2. Configurar en `.env`:
   ```
   DEEPSEEK_API_KEY=tu-api-key
   ```

### Firefly III

1. En Firefly III, ir a **Options → Profile → OAuth**
2. Crear nuevo **Personal Access Token**
3. Configurar en `.env`:
   ```
   FIREFLY_BASE_URL=http://localhost:8080
   FIREFLY_API_TOKEN=tu-token
   ```

### Variables de Entorno Clave

```bash
# Encryption key (generar con: openssl rand -hex 32)
TOKEN_ENCRYPTION_KEY=your-32-byte-hex-key

# Gmail filters (subjects to match)
GMAIL_SUBJECT_FILTERS=Factura,Pago,Recibo,Compra,Transferencia

# Processing options
DRY_RUN=false
AUTO_CREATE_ACCOUNTS=true
AUTO_CREATE_CATEGORIES=true
```

## 🎯 Uso

### Iniciar servidor

```bash
# Desarrollo
uvicorn app.main:app --reload

# Producción
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Con Docker

```bash
# Producción
docker-compose up -d

# Desarrollo
docker-compose -f docker-compose.dev.yml up
```

### Primera ejecución

1. Autenticar Gmail:
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/gmail/init
   ```
   (Se abrirá navegador para autorizar)

2. Sincronizar Firefly III:
   ```bash
   curl -X POST http://localhost:8000/api/v1/sync/all
   ```

3. Procesar emails (dry-run primero):
   ```bash
   curl -X POST http://localhost:8000/api/v1/processing/dry-run
   ```

4. Procesar de verdad:
   ```bash
   curl -X POST http://localhost:8000/api/v1/processing/batch
   ```

## 🔌 API Endpoints

### Health & Auth

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/v1/health` | Estado de todos los servicios |
| GET | `/api/v1/health/live` | Liveness probe |
| GET | `/api/v1/health/ready` | Readiness probe |
| POST | `/api/v1/auth/gmail/init` | Iniciar OAuth Gmail |
| GET | `/api/v1/auth/status` | Estado de autenticación |

### Emails

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/v1/emails` | Listar emails con filtros |
| GET | `/api/v1/emails/{id}` | Detalle de email |
| GET | `/api/v1/emails/{id}/analyze` | Analizar sin crear |

### Processing

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/api/v1/processing/batch` | Procesar lote de emails |
| POST | `/api/v1/processing/single` | Procesar email específico |
| POST | `/api/v1/processing/dry-run` | Procesar sin crear transacciones |
| POST | `/api/v1/processing/retry-failed` | Reintentar fallidos |
| GET | `/api/v1/processing/audit` | Ver logs de auditoría |
| GET | `/api/v1/processing/statistics` | Estadísticas |

### Sync

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/api/v1/sync/all` | Sincronizar todo |
| POST | `/api/v1/sync/accounts` | Sincronizar cuentas |
| POST | `/api/v1/sync/categories` | Sincronizar categorías |
| GET | `/api/v1/sync/accounts` | Ver cuentas cacheadas |
| GET | `/api/v1/sync/categories` | Ver categorías cacheadas |

## 🔄 Flujo de Procesamiento

```
1. FETCH EMAILS
   └─ Gmail API → Filtrar por subject → Excluir procesados

2. ANALYZE (por cada email)
   └─ DeepSeek AI → Extraer: amount, date, merchant, category, type

3. RESOLVE ACCOUNTS
   ├─ Source: Asset (gastos) o Revenue (ingresos)
   └─ Destination: Expense (gastos) o Asset (ingresos)
   └─ Auto-crear si no existe

4. RESOLVE CATEGORY
   └─ Buscar o crear en Firefly III

5. CREATE TRANSACTION
   └─ POST /api/v1/transactions en Firefly III

6. AUDIT
   └─ Guardar resultado en audit_logs
   └─ Marcar email como procesado (idempotencia)
```

## 🛠 Desarrollo

### Tests

```bash
pytest
pytest --cov=app
```

### Linting

```bash
ruff check app/
black app/
mypy app/
```

### Pre-commit

```bash
pre-commit install
pre-commit run --all-files
```

## ❓ Troubleshooting

### Gmail OAuth Error
- Verificar que `credentials/google_credentials.json` existe
- Verificar que Gmail API está habilitada en Google Cloud
- Eliminar `credentials/google_token.json` y re-autenticar

### Firefly Connection Error
- Verificar `FIREFLY_BASE_URL` (incluir puerto)
- Verificar que el token tiene permisos correctos
- Probar: `curl -H "Authorization: Bearer TOKEN" URL/api/v1/about`

### DeepSeek Rate Limit
- El sistema tiene reintentos automáticos
- Considerar reducir `GMAIL_MAX_RESULTS`
- Verificar límites de tu plan

### Duplicados
- El sistema usa `Message-ID` + `internal_id` para idempotencia
- Ver tabla `processed_emails` en la base de datos
- Revisar `audit_logs` para detalles

## 📄 Licencia

MIT License - ver [LICENSE](LICENSE) para detalles.
