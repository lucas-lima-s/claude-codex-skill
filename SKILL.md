---
name: codex
description: Delegar ao Codex uma das cinco operações — revisar um plano (plan-review), verificar uma implementação por git diff (verify), responder pergunta/opinião (ask), fazer retrospectiva holística da sessão (insight) ou executar uma tarefa (delegate). Use SEMPRE que o usuário disser qualquer uma destas frases (ou equivalentes naturais em pt-BR "revise esse plano com o codex", "revisa esse plano pelo codex", "pede pro codex revisar isso", "implemente esse plano com o codex", "manda o codex fazer", "delega ao codex", "pergunte ao codex o que ele acha", "pergunta pro codex sobre", "manda isso pro codex", "segunda opinião do codex", "verifica minha implementação com o codex", "pede pro codex olhar o que eu fiz", "faz um insight da sessão", "analisa o que a gente fez", "retrospectiva pelo codex", "o que a gente poderia ter feito"). A skill é o ÚNICO ponto de entrada para invocar o Codex automaticamente — não use o plugin /codex:*. Para o modo `delegate`, SEMPRE confirmar com o usuário antes de rodar porque o Codex roda com `--sandbox danger-full-access` e pode editar/deletar arquivos dentro ou fora do workspace.
argument-hint: plan-review|verify|ask|insight|delegate|batch-ask|batch-delegate|bg-start|bg-status|bg-output|bg-cancel|bg-list [args]
allowed-tools:
  - Read
  - Write
  - Bash(git status*)
  - Bash(git diff*)
  - Bash(git rev-parse*)
  - Bash(*scripts/invoke_codex_with_claude.py*)
  - Bash(*scripts/invoke_codex_with_claude.ps1*)
  - Bash(*scripts/dump_transcript_for_codex.py*)
  - Bash(*scripts/build_review_packet.py*)
  - Bash(*scripts/codex_batch.py*)
  - Bash(*scripts/codex_bg.py*)
---

# Codex — entry point único

Invoca o wrapper `~/.claude/skills/codex/scripts/invoke_codex_with_claude.py`
em um dos modos abaixo. Nunca chamar `codex` CLI direto, nunca usar o plugin
oficial `/codex:*` a partir desta skill — aquele é bypass manual explícito do
usuário, não caminho de automação.

**Reasoning por modo** (controlado pelo wrapper, não sobrescrever manualmente):

| Modo | Reasoning | Sandbox | Timeout default |
|---|---|---|---|
| `plan-review` | `xhigh` | `read-only` | 300s |
| `verify` | `medium` | `read-only` | 180s |
| `ask` | `medium` | `read-only` | 120s |
| `insight` | `xhigh` | `read-only` | 420s |
| `delegate` | `xhigh` | `danger-full-access` | 300s |

**Mapeamento de frases naturais → modo:**

| Frase do usuário (pt-BR) | Modo |
|---|---|
| "revise esse plano com o codex" / "revisa pelo codex" / "pede pro codex revisar" | `plan-review` |
| "pergunte ao codex o que ele acha" / "pergunta pro codex" / "segunda opinião do codex" | `ask` |
| "verifica minha implementação com o codex" / "pede pro codex olhar o que eu fiz" | `verify` |
| "implemente esse plano com o codex" / "manda o codex fazer" / "delega ao codex" | `delegate` |
| "faz um insight da sessão" / "analisa o que a gente fez" / "retrospectiva pelo codex" | `insight` |
| "manda isso pro codex" (ambíguo) | perguntar qual dos modos |

## Transcript da sessão (contexto conversacional)

Política por modo:

| Modo | Inclui transcript? | Turnos inline | Path completo do jsonl? |
|---|---|---|---|
| `plan-review` | SIM (sempre) | últimos 10 | não |
| `verify` | SIM (sempre) | últimos 15 | não |
| `insight` | SIM (sempre) | últimos 40 | SIM |
| `ask` | só se usuário pedir | últimos 10 | não |
| `delegate` | só se usuário pedir | últimos 10 | não |

**Como gerar:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/dump_transcript_for_codex.py" \
  --cwd "<cwd>" \
  --last-turns <N> \
  --output "$env:TEMP/codex_transcript_<timestamp>.txt"
```

`--last-turns 0` = sessão inteira (use só em `insight`).

**Quando pular:**
- Sessão nova (primeiro turno) — pula.
- Usuário pediu "sem contexto" / "ignore a conversa" — pula.
- Conversa não relacionada ao pedido — pula.

## Modo 1 — `plan-review` *(revisar um plano)*

**Quando:** usuário quer validar um plano antes de executar.

**Entrada:** caminho de arquivo do plano. Se ausente:
1. Se houver plan file do plan mode atual (system reminder), usar.
2. Se o usuário colou texto, gravar em `$env:TEMP/codex_plan_<timestamp>.md`.
3. Caso contrário, perguntar.

**Packet:** o wrapper monta o review packet automaticamente quando recebe
`--last-message-file` (resolve arquivos citados, aplica janela ±50, manifest,
truncamento em 120 KB). Para forçar um packet customizado, gere antes via
`scripts/build_review_packet.py` e passe `--review-packet-file`.

**Execução:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" plan-review \
  --cwd "<cwd>" \
  --target-path "<subpasta opcional>" \
  --last-message-file "<path do plano>" \
  --transcript-file "<dump dos últimos 10 turnos>"
```

## Modo 2 — `verify` *(revisar uma implementação)*

**Quando:** usuário acabou de editar e quer o Codex olhar o diff.

**Payload:** JSON em `$env:TEMP` com:

```json
{
  "cwd": "<cwd>",
  "last_assistant_message": "<descrição do que foi feito ou vazio>",
  "transcript_path": "",
  "git_status_short": "<git status --short>",
  "git_diff_worktree": "<git diff --no-ext-diff --relative HEAD -->",
  "git_diff_cached": "<git diff --no-ext-diff --relative --cached>",
  "changed_files_from_transcript": []
}
```

**Execução:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" verify \
  --cwd "<cwd>" \
  --payload-file "<path/payload.json>" \
  --transcript-file "<dump 15 turnos>"
```

## Modo 3 — `ask` *(pergunta / opinião)*

**Quando:** opinião do Codex sobre texto / dúvida técnica geral.

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" ask \
  --cwd "<cwd>" \
  --target-path "<subpasta opcional>" \
  --question-file "<path da pergunta>"
```

## Modo 4 — `insight` *(retrospectiva)*

**Quando:** análise estratégica — o que foi feito, gaps, próximos passos. NÃO é caça-bugs.

**Obrigatório:** `--transcript-file` com a sessão inteira (`--last-turns 0`).

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" insight \
  --cwd "<cwd>" \
  --focus-file "<path do foco (opcional)>" \
  --transcript-file "<dump completo>" \
  --transcript-jsonl-path "<path absoluto do jsonl da sessão>"
```

## Modo 5 — `delegate` *(Codex executa uma tarefa)*

**Quando:** usuário pede explicitamente "manda o codex fazer", "delega ao codex".

**Não usar** se a tarefa é ambígua (peça `plan-review` antes) ou se o usuário
quer Claude fazendo.

### Sandbox

`delegate` roda com `--sandbox danger-full-access`. Codex pode criar, editar
ou deletar **qualquer arquivo na máquina, dentro ou fora do `--cwd`**.

### Confirmação obrigatória — 5 campos

Antes de chamar o wrapper, exibir ao usuário **todos os cinco**:

1. **Tarefa literal** que será enviada ao Codex (texto integral do `task-file`).
2. **`cwd` e branch** alvo (incluir `git rev-parse --abbrev-ref HEAD`).
3. **Aviso explícito**: "Codex roda com `danger-full-access`. Pode criar,
   editar ou deletar arquivos em qualquer lugar do disco, não só em `<cwd>`."
4. **Paths fora do workspace** que Claude consegue inferir do texto da
   tarefa (qualquer path absoluto fora de `<cwd>`, qualquer menção a
   `~`/`$HOME`/`$env:TEMP`/`C:\` fora do projeto).
5. **Palavras de risco** detectadas no texto da tarefa: `delete`, `drop`,
   `rm -rf`, `force`, `reset --hard`, `truncate`, `--no-verify`. Listar uma
   por uma. Se nenhuma for encontrada, dizer "nenhuma palavra de risco
   detectada".

Após exibir, perguntar: **"Confirma a execução com `danger-full-access`?
(sim/não)"**. Só prosseguir com resposta afirmativa.

### Execução

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" delegate \
  --cwd "<cwd>" \
  --target-path "<subpasta opcional>" \
  --task-file "<path/task.txt>" \
  [--transcript-file "<dump (apenas se usuário pediu contexto)>"]
```

### Output esperado

JSON do wrapper inclui (além dos campos canônicos):

```
files_created   [str]   arquivos novos
files_edited    [str]   arquivos modificados
files_deleted   [str]   arquivos removidos
commands_run    [str]   comandos shell executados
tests_run       [str]   testes/verificações executadas
```

Após apresentar o resultado, rodar `git status --short` e mostrar em bloco de
código (único `code fence` permitido na apresentação — output literal de
comando).

### Protocolo `needs_input`

O Codex pode interromper com `status=needs_input` e devolver `questions`
(lista de `{id, question, context}`). Quando isso acontecer:

1. **Não escalar imediatamente.** Tentar resolver localmente com até **2
   passadas** de Read/Grep/Glob/Bash. Cada passada deve apontar para uma
   pergunta específica.
2. Se as 2 passadas resolverem todas as perguntas, **retomar a chamada**:
   gerar um novo `task-file` que inclua o contexto anterior (tarefa
   original + perguntas + respostas obtidas) e disparar `delegate`
   novamente. Não usar `--resume` do Codex; o estado é todo recarregado via
   prompt.
3. Se algum item exigir julgamento humano (preferência de design, decisão
   de produto, autorização extra), **escalar ao usuário** com a pergunta
   exata e o contexto que Claude já levantou — não dispare a retomada por
   conta própria.

## Modo 6 — `batch-ask` *(perguntas em paralelo)*

Read-only. Roda até 4 perguntas em paralelo via `scripts/codex_batch.py`.
Falha de um item não cancela os demais. Resposta agregada com `partial=true`
quando houver erro parcial. Detalhes em `scripts/codex_batch.py --help`.

## Modo 7 — `batch-delegate` *(delegate em paralelo com write-set)*

Cada item declara um write-set (lista de paths). O batcher rejeita execução
quando dois write-sets se sobrepõem. Após cada execução, compara write-set
declarado vs `files_created/edited/deleted` reportados; marca
`write_set_violated=true` quando o Codex extrapola.

## Modos em background — `bg-start | bg-status | bg-output | bg-cancel | bg-list`

Quando uma chamada do wrapper deve demorar (ex.: `delegate` >60s, `insight`
>5min) e não convém bloquear a sessão, usar `scripts/codex_bg.py`:

| Subcomando | Função |
|---|---|
| `bg-start <mode> [...args]` | Spawn destacado do wrapper. Retorna imediatamente com `run_id` e `pid`. |
| `bg-status <run_id>` | Estado atual: `running \| done \| error \| cancelled`. |
| `bg-output <run_id>` | JSON canônico do wrapper (mesmo schema dos modos síncronos) quando `done`. |
| `bg-cancel <run_id>` | Mata o subprocesso e marca a run como `cancelled`. |
| `bg-list [--limit N]` | Lista runs ativas e recentes. |

**Regras operacionais:**
- `bg-start` retorna controle imediatamente. **SEMPRE** exibir o `run_id`
  ao usuário no momento do start, para que ele possa retomar com
  `bg-status <run_id>` mesmo em sessões futuras.
- Limite default de **5 runs simultâneas**. Configurável via
  `--max-concurrent N` ou `CODEX_BG_MAX_CONCURRENT`. Quando o limite é
  atingido, `bg-start` recusa com `status=error, reason=max_concurrent_reached`.
- `bg-cancel` é idempotente.
- Cleanup automático: runs terminadas com `mtime > 7 dias` são removidas.

**Exemplo:**

```bash
# disparar
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" \
  start delegate \
  --cwd "<cwd>" \
  --task-file "<task.txt>"
# {"status": "ok", "run_id": "abc123def456", "pid": 1234, ...}

# checar mais tarde
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" status abc123def456

# coletar quando done
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" output abc123def456
```

A saída de `bg-output` é o JSON canônico do wrapper — apresentar ao
usuário com a mesma formatação dos modos síncronos (tabela meta + lista
numerada de findings traduzidos).

## Saída (comum aos modos individuais)

```
status             "ok" | "error" | "needs_input"
severity           "low" | "medium" | "high"
confidence         "low" | "medium" | "high"
summary            string
findings           [ {severity, category, title, detail, location} ]
block_recommended  bool
fingerprint        16 hex chars
duration_seconds   float
mode               "plan-review" | ...
degraded           bool (opcional, set quando salvamos JSON parcial)
questions          [ {id, question, context} ] (status=needs_input)
files_*            (delegate)
```

### Apresentação ao usuário

Apresentar SEMPRE em markdown renderizado (headers, tabela, citação,
negrito). NUNCA envolver a apresentação em code fence. SEMPRE traduzir
`summary`, `title`, `detail` e `location` para pt-BR — não manter o original
em inglês ao lado.

**Modos `plan-review`, `verify`, `delegate` (status=ok):**

1. Cabeçalho: `**Codex {mode}**`
2. Tabela de meta:

   | campo | valor |
   |---|---|
   | duração | {X}s |
   | severity | {S} |
   | confidence | {C} |
   | findings | {N} |

3. Summary em citação: `> {summary}`
4. Findings em lista numerada:

   ```
   1. **[{severity}] {title}** — `{location}`
      > {detail}
   ```

   Se vazio: `_Nenhum finding._`

**Modo `ask` (status=ok):** tabela enxuta sem severity, resposta na citação.

**Modo `insight` (status=ok):** rótulos `must-do`/`should-consider`/
`nice-to-have` — não usar "bug/problema".

**status=needs_input (qualquer modo):**

1. Cabeçalho: `**Codex {mode}** — perguntas em aberto`
2. Tabela de meta padrão.
3. Summary em citação.
4. Lista numerada de `questions` (id, pergunta, contexto). Em seguida,
   aplicar o protocolo do modo `delegate` (2 passadas locais → escalar se
   persistir).

**Todos os modos, status=error:** cabeçalho com `— **ERRO**`, mesma tabela,
summary em citação, perguntar se tenta de novo.

## Regras invioláveis

- **Nunca** chamar `codex` CLI direto.
- **Nunca** invocar `/codex:*` (plugin oficial) a partir desta skill.
- **Nunca** aplicar findings como edit automático — só apresentar.
- **Temporários** (payload.json, task.txt, packet, transcripts) **só** em
  `$env:TEMP`; zero writes dentro do repo-alvo.
- Em `delegate`, confirmação explícita do usuário com os 5 campos é
  obrigatória antes do wrapper. Sem essa confirmação, não invocar.
- Apresentar TODOS os findings traduzidos para pt-BR antes de qualquer
  análise própria (regra global "Codex Review Transparency"). Não omita,
  resuma ou agrupe.
- Reasoning padrão é controlado pelo wrapper conforme o modo. Sobrescrita
  só via `--reasoning-effort {low|medium|high|xhigh}` explícito do
  wrapper, **e somente quando o usuário pediu explicitamente** (ex.:
  "revisa em effort máximo", "roda em low pra ser rápido"). Não usar
  `-c model_reasoning_effort=...` direto na CLI do Codex.
- Apresentação SEMPRE em markdown renderizado, NUNCA dentro de code fence
  — exceto o output de `git status --short` no modo `delegate`.
