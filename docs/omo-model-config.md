# Configuración de modelos de IA — agentes OMO (oh-my-openagent)

Guía para cambiar los modelos de los agentes de OMO (plugin `oh-my-openagent` para
OpenCode) usando **solo el proveedor `opencode-go`**, con criterio de
estabilidad precio/calidad y respeto a los rate limits.

---

## 1. Dónde vive la configuración

| Scope | Archivo | Precedencia |
|---|---|---|
| Usuario (global) | `~/.omo/omo.jsonc` | baja |
| Proyecto | `<proyecto>/.omo/omo.jsonc` | **alta** (el más cercano al cwd gana) |

- Formato JSONC (admite comentarios y comas finales).
- Archivos legacy (`oh-my-openagent.jsonc`, `oh-my-opencode.jsonc`) ya **no se
  leen** en runtime (se migran una sola vez).
- Los cambios requieren **reiniciar OpenCode** para que el plugin re-resuelva
  los agentes.

El archivo actual del usuario es `~/.omo/omo.jsonc` con el bloque `[opencode]`.
El esquema oficial:
`https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/assets/omo.schema.json`

## 2. Estructura del archivo

```jsonc
// ~/.omo/omo.jsonc
{
  "$schema": "https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/assets/omo.schema.json",
  "[opencode]": {
    "agents": {
      "<nombre-agente>": {
        "model": "opencode-go/<modelo>",     // obligatorio: provider/model
        "reasoning": "max"                    // opcional: off|minimal|low|medium|high|xhigh|max|auto
      }
    },
    "categories": {
      "<nombre-categoria>": {
        "model": "opencode-go/<modelo>"
      }
    }
  }
}
```

### Campos de un agente/categoría

| Campo | Tipo | Notas |
|---|---|---|
| `model` | `string` | `provider/model`. **El prefijo `provider/` es obligatorio** (`parseModelString` rechaza IDs sueltos). |
| `models` | `array` | Cadena ordenada de fallback: `["opencode-go/a", { "model": "opencode-go/b", "reasoning": "high" }]`. |
| `reasoning` | `string` | Campo **canónico** de esfuerzo de razonamiento. |
| `variant` / `reasoningEffort` / `fallback_models` | deprecated | Se auto-normalizan a `reasoning` / `models`. Siguen funcionando. |
| `allow_non_gpt_model` | `boolean` | **Solo válido en `hephaestus`** (ver gotchas). |

## 3. Todos los agentes y categorías

### Agentes (claves de `agents`)

| Clave | Rol | Frecuencia típica |
|---|---|---|
| `sisyphus` | Orquestador principal (agente raíz de la sesión) | Alta |
| `sisyphus-junior` | Ejecutor delegado vía `task(category=...)` | Muy alta |
| `build` | Builder por defecto de OpenCode (fallback) | Baja |
| `plan` | Agente de planificación de OpenCode | Media |
| `prometheus` | Planificador de OMO (ulw-plan) | Media |
| `oracle` | Consultor read-only de alto IQ (debug/arquitectura) | Baja |
| `metis` | Consultor pre-planificación (ambigüedad de requisitos) | Baja |
| `momus` | Crítico de planes (clarity/verificabilidad) | Baja |
| `explore` | Grep contextual del codebase (background) | Muy alta |
| `librarian` | Investigación externa (docs, GitHub, web) | Media |
| `multimodal-looker` | Análisis de PDFs/imágenes/media | Baja |
| `atlas` | Orquestador alternativo (rol similar a sisyphus) | Media-baja |
| `hephaestus` | Builder especializado (**solo modelos GPT**, ver gotchas) | Baja |
| `general` | Agente genérico de OpenCode (no lo gestiona OMO) | — |

### Categorías (claves de `categories`, usadas con `task(category=...)`)

| Clave | Rol | Frecuencia típica |
|---|---|---|
| `visual-engineering` | Frontend, UI/UX, styling, diseño | Media |
| `ultrabrain` | Lógica dura, arquitectura, algoritmos | Rara |
| `deep` | Investigación autónoma + implementación e2e | Alta |
| `artistry` | Resolución creativa no convencional | Muy rara |
| `quick` | Tareas triviales (1 archivo, typos) | Alta |
| `unspecified-low` | Tareas de bajo esfuerzo | Media-alta |
| `unspecified-high` | Tareas de alto esfuerzo no categorizadas | Media |
| `writing` | Documentación, prosa técnica | Baja-media |

### Agentes de usuario (fuera de OMO)

`~/.config/opencode/agents/*.md` — agentes propios del usuario:

| Archivo | Estado tras aplicar (2026-08-16) |
|---|---|
| `cavecrew-investigator.md` | ✅ `model: opencode-go/deepseek-v4-flash` |
| `cavecrew-reviewer.md` | ✅ `model: opencode-go/deepseek-v4-flash` |
| `cavecrew-builder.md` | sin `model` (hereda el default) |

## 4. Catálogo de modelos `opencode-go` (19 disponibles)

Datos: rate limits (5h / semana / mes), precio USD por millón de tokens
(input/output), visión, variants de razonamiento declaradas.

| Modelo (ID) | 5h | Semana | Mes | $ in/out | Visión | Variants |
|---|---|---|---|---|---|---|
| `mimo-v2.5` | 30,100 | 75,200 | 150,400 | 0.14/0.28 | ✅ | — |
| `qwen3.7-plus` | 4,300 | 10,800 | 21,600 | 0.40/1.60 | ✅ | — |
| `hy3` | 4,300 | 10,750 | 21,500 | 0.14/0.58 | ❌ | none/low/high |
| `minimax-m2.7` | 3,400 | 8,500 | 17,000 | 0.30/1.20 | ❌ | — |
| `qwen3.6-plus` | 3,300 | 8,200 | 16,300 | 0.50/3.00 | ✅ | — |
| `mimo-v2.5-pro` | 3,250 | 8,150 | 16,300 | 0.44/0.87 | ✅ | — |
| `minimax-m3` | 3,200 | 8,000 | 16,000 | 0.30/1.20 | ✅ | — |
| `deepseek-v4-flash` | 3,800 | 9,450 | 18,900 | 0.22/0.66 | ❌ | low/high/max |
| `gpt-5.6-luna` | 2,050 | 5,100 | 10,250 | 0.10/0.60 | ✅ + PDF | none..max |
| `kimi-k2.7-code` | 1,350 | 3,380 | 6,750 | 0.95/4.00 | ✅ | — |
| `kimi-k2.6` | 1,150 | 2,880 | 5,750 | 0.95/4.00 | ✅ | — |
| `deepseek-v4-pro` | 1,050 | 2,600 | 5,200 | 0.66/1.98 | ❌ | high/max |
| `glm-5.2` | 880 | 2,150 | 4,300 | 1.40/4.40 | ❌ | high/max |
| `glm-5.1` | 880 | 2,150 | 4,300 | 1.40/4.40 | ❌ | — |
| `qwen3.7-max` | 340 | 840 | 1,690 | 2.50/7.50 | ❌ | — |
| `glm-5.3` | 220 | 540 | 1,080 | 1.40/4.40 | ❌ | low/high/max |
| `qwen3.8-max` | 160 | 400 | 810 | 2.00/6.00 | ✅ | — |
| `grok-4.5` | 120 | 300 | 600 | 2.00/6.00 | ✅ | low/med/high |
| `kimi-k3` | 110 | 250 | 490 | 3.00/15.00 | ✅ | max |

⚠️ **`kimi-k3` (110 req/5h, $3/$15) es una bomba de rate limit** — es el default
del plugin para muchos agentes pero no aguanta uso frecuente. Evitarlo salvo
casos rarísimos.

## 5. Cómo se resuelve el modelo (prioridad)

```
1. Modelo elegido en TUI (uiSelectedModel)
2. "model" explícito en config (agents/categories)  ← GANA SIEMPRE si está
3. "models"/fallback_models del usuario
4. Cadena hardcodeada del plugin (fallbackChain)
5. Default del sistema (modelo de opencode.json / TUI)
```

- Un `model` explícito en config se usa **tal cual**, sin verificación de
  disponibilidad (si el provider no está conectado, falla en runtime).
- Si una categoría builtin no resuelve ningún modelo de su cadena, se oculta de
  `availableCategories` (spawn falla con `model_unavailable`). Forzar `model`
  explícito es la vía para habilitarla.

### Forzar un único proveedor: `disabled_providers`

Filtra proveedores de **todas** las cadenas de fallback en tiempo de carga:

```jsonc
{
  "[opencode]": {
    "disabled_providers": ["anthropic", "openai", "google", "xai", "deepseek",
      "github-copilot", "vercel", "kimi-for-coding", "moonshotai",
      "zai-coding-plan", "bailian-coding-plan", "qwen-token-plan",
      "alibaba-token-plan", "minimax-coding-plan", "minimax-cn-coding-plan",
      "quotio-openai", "xiaomi", "firmware", "ollama-cloud", "aihubmix",
      "anthropic-api", "openrouter", "moonshotai-cn"]
  }
}
```

⚠️ Si un agente usa un provider deshabilitado y **no** tiene `model` explícito
con entrada permitida, se produce error de carga. Por eso, con
`disabled_providers` hay que fijar `model` explícito en todos los agentes.

## 6. Gotchas

1. **`sisyphus` solo está verificado por los maintainers en**: Claude
   (Fable 5/Opus 5/Sonnet 5), Kimi (K3/K2.7), GLM (5.2/5.1), GPT (5.4/5.5/5.6 Sol).
   Qwen/MiMo/DeepSeek/MiniMax como sisyphus = **desaconsejado** (puede degradar
   el orquestador). El hook `no-sisyphus-gpt` además redirige sisyphus→hephaestus
   con GPT no nativo.
2. **`hephaestus` es GPT-only**: el registro del agente lo **omite** si el modelo
   no matchea `/^gpt-5[.-](3-codex|4|5|6)/`. En `opencode-go` el único modelo que
   pasa es **`gpt-5.6-luna`**. `allow_non_gpt_model: true` solo baja el toast de
   error a warning (el registro igual se omite con modelos no-GPT).
3. **`reasoning`/`variant` solo si el modelo lo declara**: si el modelo no tiene
   variants (ej. `kimi-k2.7-code`, `mimo-v2.5`, `qwen3.7-plus`), omitir el campo.
   `hy3` no tiene `max` (solo none/low/high). `deepseek-v4-pro` no tiene `low`.
4. **Prefijo obligatorio**: `"model": "deepseek-v4-flash"` es inválido; debe ser
   `"opencode-go/deepseek-v4-flash"`.
5. **Restart** de OpenCode para aplicar.
6. Verificación: `bunx oh-my-openagent doctor --verbose` muestra la resolución
   efectiva por agente/categoría; `opencode models` lista los modelos activos.

## 7. Asignación aplicada (solo opencode-go) — 2026-08-16

### Agentes

| Agente | Modelo | Reasoning | Rate 5h | $ in/out |
|---|---|---|---|---|
| `sisyphus` | `opencode-go/glm-5.2` | `high` | 880 | 1.40/4.40 |
| `sisyphus-junior` | `opencode-go/deepseek-v4-flash` | `max` | 3,800 | 0.22/0.66 |
| `build` | `opencode-go/deepseek-v4-flash` | `max` | 3,800 | 0.22/0.66 |
| `plan` | `opencode-go/hy3` | `high` | 4,300 | 0.14/0.58 |
| `prometheus` | `opencode-go/hy3` | `high` | 4,300 | 0.14/0.58 |
| `oracle` | `opencode-go/deepseek-v4-pro` | `max` | 1,050 | 0.66/1.98 |
| `metis` | `opencode-go/kimi-k2.6` | — | 1,150 | 0.95/4.00 |
| `momus` | `opencode-go/glm-5.2` | `high` | 880 | 1.40/4.40 |
| `atlas` | `opencode-go/glm-5.2` | `high` | 880 | 1.40/4.40 |
| `explore` | `opencode-go/mimo-v2.5` | — | 30,100 | 0.14/0.28 |
| `librarian` | `opencode-go/qwen3.6-plus` | — | 3,300 | 0.50/3.00 |
| `multimodal-looker` | `opencode-go/gpt-5.6-luna` | `high` | 2,050 | 0.10/0.60 |
| `hephaestus` | `opencode-go/gpt-5.6-luna` | `medium` | 2,050 | 0.10/0.60 |

### Categorías

| Categoría | Modelo | Reasoning | Rate 5h | $ in/out |
|---|---|---|---|---|
| `visual-engineering` | `opencode-go/qwen3.7-plus` | — | 4,300 | 0.40/1.60 |
| `ultrabrain` | `opencode-go/deepseek-v4-pro` | `max` | 1,050 | 0.66/1.98 |
| `deep` | `opencode-go/minimax-m3` | — | 3,200 | 0.30/1.20 |
| `artistry` | `opencode-go/glm-5.2` | `high` | 880 | 1.40/4.40 |
| `quick` | `opencode-go/minimax-m2.7` | — | 3,400 | 0.30/1.20 |
| `unspecified-low` | `opencode-go/minimax-m3` | — | 3,200 | 0.30/1.20 |
| `unspecified-high` | `opencode-go/hy3` | `high` | 4,300 | 0.14/0.58 |
| `writing` | `opencode-go/gpt-5.6-luna` | `high` | 2,050 | 0.10/0.60 |

### Bloque de configuración aplicado

```jsonc
// ~/.omo/omo.jsonc
{
  "[opencode]": {
    "$schema": "https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/assets/omo.schema.json",
    "disabled_providers": ["anthropic", "openai", "google", "xai", "deepseek",
      "github-copilot", "vercel", "kimi-for-coding", "moonshotai",
      "zai-coding-plan", "bailian-coding-plan", "qwen-token-plan",
      "alibaba-token-plan", "minimax-coding-plan", "minimax-cn-coding-plan",
      "quotio-openai", "xiaomi", "firmware", "ollama-cloud", "aihubmix",
      "anthropic-api", "openrouter", "moonshotai-cn"],
    "agents": {
      "sisyphus":          { "model": "opencode-go/glm-5.2", "reasoning": "high" },
      "atlas":             { "model": "opencode-go/glm-5.2", "reasoning": "high" },
      "sisyphus-junior":   { "model": "opencode-go/deepseek-v4-flash", "reasoning": "max" },
      "build":             { "model": "opencode-go/deepseek-v4-flash", "reasoning": "max" },
      "plan":              { "model": "opencode-go/hy3", "reasoning": "high" },
      "prometheus":        { "model": "opencode-go/hy3", "reasoning": "high" },
      "oracle":            { "model": "opencode-go/deepseek-v4-pro", "reasoning": "max" },
      "metis":             { "model": "opencode-go/kimi-k2.6" },
      "momus":             { "model": "opencode-go/glm-5.2", "reasoning": "high" },
      "explore":           { "model": "opencode-go/mimo-v2.5" },
      "librarian":         { "model": "opencode-go/qwen3.6-plus" },
      "multimodal-looker": { "model": "opencode-go/gpt-5.6-luna", "reasoning": "high" },
      "hephaestus":        { "model": "opencode-go/gpt-5.6-luna", "reasoning": "medium" }
    },
    "categories": {
      "visual-engineering": { "model": "opencode-go/qwen3.7-plus" },
      "ultrabrain":         { "model": "opencode-go/deepseek-v4-pro", "reasoning": "max" },
      "deep":               { "model": "opencode-go/minimax-m3" },
      "artistry":           { "model": "opencode-go/glm-5.2", "reasoning": "high" },
      "quick":              { "model": "opencode-go/minimax-m2.7" },
      "unspecified-low":    { "model": "opencode-go/minimax-m3" },
      "unspecified-high":   { "model": "opencode-go/hy3", "reasoning": "high" },
      "writing":            { "model": "opencode-go/gpt-5.6-luna", "reasoning": "high" }
    }
  }
}
```

### Lógica de la decisión (verificada con benchmarks)

1. **Orquestación** (sisyphus, atlas, momus) → set verificado por maintainers:
   `glm-5.2` (AA Index 53, Terminal-Bench 81, 1M ctx, MIT, benchmarks independientes).
   `kimi-k2.7-code` se descartó: cero benchmarks independientes (todo propietario de Moonshot).
2. **Ejecución de alto volumen** (sisyphus-junior, build) → `deepseek-v4-flash`
   (workhorse probado: Terminal-Bench 82.7, DeepSWE 54.4, vals.ai SWE 91%, $0.22/$0.66).
3. **Planificación** (plan, prometheus, unspecified-high) → `hy3` (Tencent, tool-call
   estable con varianza <4%, token-eficiente, $0.14/$0.58).
4. **Razonamiento duro de bajo volumen** (oracle, ultrabrain) → `deepseek-v4-pro`
   (GPQA 90.1, vals.ai SWE 96.4%, AA 53).
5. **Búsqueda de alto volumen** (explore) → `mimo-v2.5` (30,100 req/5h, $0.14/$0.28).
   Se evitó para ediciones de código (LiveCodeBench 35.5 — débil).
6. **Ediciones triviales** (quick) → `minimax-m2.7` (AA 50, SWE-Pro 56 — fiable).
7. **Trabajo pesado autónomo** (deep, unspecified-low) → `minimax-m3`
   (frontier-adjacent, SWE-Pro 59, 1M ctx, $0.30/$1.20).
8. **Investigación** (librarian) → `qwen3.6-plus` (AA 40 — mejor que 3.7-plus que da 39).
9. **Visión/PDF/prosa** (multimodal-looker, writing) → `gpt-5.6-luna` ($0.10/$0.60, AA 51,
   Coding Agent 75). También hephaestus: única opencode-go que pasa el regex GPT.
10. **Frontend/UI** (visual-engineering) → `qwen3.7-plus` (visión, 4,300 req/5h).
11. `kimi-k3`, `grok-4.5`, `qwen3.8-max`, `qwen3.7-max`, `glm-5.1`, `glm-5.3`,
    `mimo-v2.5-pro`, `kimi-k2.7-code` quedan **sin usar** (rate bajo, precio alto,
    sin benchmarks, o enfoque no adecuado).

### Costo estimado — sesión de refactor (proyecto pequeño/mediano)

~94 requests, ~2.42M tokens in / 247k out → **≈ $2.68 por sesión** (83% = orquestador).
Peor uso de rate: sisyphus 40/880 (4.5%) — 10 sesiones en 5h sin tocar techo.
vs config anterior (todo deepseek): ~$1.50/sesión pero 10 roles sobre
`deepseek-v4-pro` (1,050 req/5h) → riesgo de agotamiento en días intensos.

---

## Historial

- 2026-08-16: **Aplicada asignación equilibrada** (13 agentes + 8 categorías en
  opencode-go, `disabled_providers` activo, cavecrew agents → deepseek-v4-flash).
  Backup previo: `~/.omo/omo.jsonc.bak.2026-08-16-pre-model-swap`.
- Estado previo (2026-08-06): todo en `opencode-go/deepseek-v4-pro` (max) y
  `opencode-go/deepseek-v4-flash` (max) — riesgo de agotar 1,050 req/5h de
  deepseek-v4-pro con 10+ consumidores.

## Verificación tras aplicar

```bash
bunx oh-my-openagent doctor --verbose   # resolución efectiva por agente/categoría
opencode models                          # confirmar modelos opencode-go activos
```