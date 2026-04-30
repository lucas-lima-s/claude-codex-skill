# Setup — skill `codex`

Ponte Claude Code → Codex CLI. Único ponto de entrada para automação.

## Requisitos

- Codex CLI instalado e no `PATH` (`codex --version` deve responder).
- Python disponível via `$SKILLS_PYTHON` (preferido), `$CLAUDE_AUTOMATION_PYTHON`,
  ou simplesmente `python`/`python3` no `PATH`.
- Credencial `COMPOSIO_API_KEY` no arquivo global `~/.claude/credentials.env`
  (lida automaticamente pelo wrapper para o subprocesso do Codex; nunca é
  exibida em logs).

## Variáveis de ambiente reconhecidas

| Variável | Função |
|---|---|
| `SKILLS_PYTHON` | Interpretador Python preferido. |
| `CLAUDE_AUTOMATION_PYTHON` | Fallback de Python. |
| `CODEX_WRAPPER_TIMEOUT_SECONDS` | Override global do timeout (segundos). Sobrescreve o default por modo. |
| `CODEX_WRAPPER_CODEX_OVERRIDE` | Aponta para um Python alternativo a ser invocado no lugar do `codex` real (usado apenas em testes — ver `tests/fake_codex.py`). |
| `CODEX_WRAPPER_DISABLE_HEARTBEAT` | Quando `1`, desliga o heartbeat de progresso no `stderr`. |
| `CODEX_WRAPPER_USE_JSON_STREAM` | Quando `1`, tenta `codex exec --json` (stream de eventos) com fallback automático para o modo padrão. |
| `CODEX_WRAPPER_TELEMETRY_DISABLED` | Quando `1`, não grava em `cache/runs.jsonl`. |
| `CODEX_BG_MAX_CONCURRENT` | Limite de runs simultâneas em background (default 5). Override via `--max-concurrent N` no `codex_bg.py start`. |
| `CODEX_DIALOGUE_MAX_TURNS` | Default de turnos no `codex_dialogue.py start` (default 3, range 1-20). Override via `--max-turns N`. |
| `CODEX_LOCALE` | Idioma das mensagens user-facing (`pt-BR` ou `en-US`). Default: detectado pelo sistema (`locale.getdefaultlocale()`), com fallback para `pt-BR`. |

## Estrutura

```
~/.claude/skills/codex/
  SKILL.md                       — entrada da skill (modos, exemplos, regras)
  SETUP.md                       — este arquivo
  scripts/
    invoke_codex_with_claude.py  — wrapper canônico (Python)
    invoke_codex_with_claude.ps1 — shim de compatibilidade (PowerShell)
    collect_claude_context.py    — coleta CLAUDE.md global/repo/target
    dump_transcript_for_codex.py — dump filtrado do transcript da sessão
    build_review_packet.py       — monta packet para `plan-review`
    normalize_codex_result.py    — normaliza saída crua do Codex
    codex_batch.py               — runner síncrono de batch-ask/batch-delegate
    codex_bg.py                  — runner assíncrono (start/status/output/cancel/list)
    codex_dialogue.py            — diálogo iterativo multi-jogada (start/next-turn/finish/abort/status)
    analyze_plan_complexity.py   — heurística para auto-sugerir plan-review-iter
    codex_config.py              — config centralizada + i18n (get/t/detect_locale)
    codex_output_schema.json     — JSON Schema usado em `--output-schema`
  config.default.json            — settings + locales versionados
  config.local.json              — override do usuário (gitignored, opcional)
  cache/
    runs.jsonl                   — telemetria (rotaciona aos 5 MB para .1)
  tests/
    fake_codex.py                — Codex falso parametrizável para testes
```

## Configuração e i18n

A skill carrega settings + locales de `config.default.json` (versionado) e
aplica deep-merge de `config.local.json` (gitignored, opcional) por cima.
Helpers em `scripts/codex_config.py`:

- `get("dialogue.default_max_turns", 3)` — lookup com dotted-path em
  `settings.*`.
- `t("wrapper.error.timeout", timeout=30)` — tradução com fallback
  pt-BR → chave literal e suporte a `{kwargs}`.
- `detect_locale()` — `CODEX_LOCALE` env > `locale.getdefaultlocale()` >
  `pt-BR`. Códigos com underscore (`pt_BR`) viram hífen (`pt-BR`).

**Para customizar**: copie campos de `config.default.json` para um novo
`config.local.json`. Apenas as chaves presentes no override são
sobrescritas (deep-merge).

Idiomas suportados out-of-the-box: `pt-BR` (default) e `en-US`. Para
adicionar um novo locale, basta adicionar uma seção `locales.<código>`
no `config.local.json` (ou propor PR pra entrar no default).

## Modos

Detalhes em `SKILL.md`. Resumo:

- `plan-review` — Codex revisa um plano (read-only).
- `verify` — Codex revisa um diff (read-only).
- `ask` — Codex responde uma pergunta direta.
- `insight` — Retrospectiva holística da sessão.
- `delegate` — Codex executa uma tarefa (`--sandbox danger-full-access`,
  exige confirmação explícita).
- `batch-ask` — Roda múltiplas perguntas em paralelo (read-only).
- `batch-delegate` — Múltiplas execuções paralelas com write-set declarado.

## Roadmap (não implementado)

Veja `ROADMAP.md` na raiz da skill. Itens em aberto:

- Cache de revisões por fingerprint, modo repro, parser de stream `--json`.

Já implementado: background agents (`scripts/codex_bg.py`),
multi-jogada (`scripts/codex_dialogue.py` + `analyze_plan_complexity.py`),
`--reasoning-effort` configurável.

## Notas para outros usuários

A skill é portátil: caminhos vêm de `$SKILLS_PYTHON`, `$USERPROFILE` e
`$env:TEMP`. Não há paths hardcoded para a máquina do autor. Se você não
tiver `$SKILLS_PYTHON`, exporte a variável (ou use `python` no `PATH`).

## Plugin oficial `codex@openai-codex`

Foi desabilitado em `~/.claude/settings.json` para evitar dois pontos de
entrada concorrentes. A invocação manual via `/codex:*` continua possível
quando o usuário habilitar de novo, mas não deve ser usada como caminho de
automação por nenhuma skill ou hook.
