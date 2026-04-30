# claude-codex-skill

Skill do [Claude Code](https://claude.com/claude-code) que serve como ponte
entre o Claude e o [Codex CLI](https://github.com/openai/codex), oferecendo
sete modos de operação para revisão, verificação, perguntas e delegação de
tarefas — com sandbox controlado, telemetria e batching.

## O que é

Um único ponto de entrada (`SKILL.md` + wrapper Python) para o Claude invocar
o Codex de forma estruturada. Os prompts, schemas, timeouts e nível de
reasoning são definidos por modo — o Claude escolhe o modo certo a partir do
pedido do usuário em pt-BR e o wrapper cuida do resto (montagem do packet,
chamada ao `codex exec`, normalização do JSON de saída e telemetria).

## Modos

| Modo             | Reasoning | Sandbox                | Timeout | Uso típico                                              |
|------------------|-----------|------------------------|---------|---------------------------------------------------------|
| `plan-review`    | xhigh     | read-only              | 300s    | Revisar um plano antes da implementação                 |
| `verify`         | medium    | read-only              | 180s    | Revisar uma implementação por `git diff`                |
| `ask`            | medium    | read-only              | 120s    | Pergunta direta / segunda opinião                       |
| `insight`        | xhigh     | read-only              | 420s    | Retrospectiva holística da sessão                       |
| `delegate`       | xhigh     | **danger-full-access** | 300s    | Codex executa uma tarefa (com confirmação obrigatória)  |
| `batch-ask`      | medium    | read-only              | —       | Até 4 perguntas em paralelo                             |
| `batch-delegate` | xhigh     | danger-full-access     | —       | Múltiplas execuções paralelas com write-set declarado   |

## Instalação

```bash
git clone https://github.com/lucas-lima-s/claude-codex-skill.git ~/.claude/skills/codex
```

A skill é portátil: caminhos vêm de variáveis de ambiente
(`$SKILLS_PYTHON`, `$USERPROFILE`, `$env:TEMP`). Não há paths hardcoded.

### Pré-requisitos

- [Codex CLI](https://github.com/openai/codex) instalado e no `PATH`
  (`codex --version` deve responder).
- Python 3.10+ disponível via `$SKILLS_PYTHON`, `$CLAUDE_AUTOMATION_PYTHON`,
  ou simplesmente `python` / `python3` no `PATH`.
- Claude Code (a skill foi desenhada para ser invocada pelo Claude, mas os
  scripts em `scripts/` podem ser usados standalone).

Detalhes em [`SETUP.md`](SETUP.md).

## Uso

Depois de instalada, o Claude reconhece automaticamente frases naturais como:

- "revise esse plano com o codex" → `plan-review`
- "pergunta pro codex o que ele acha" → `ask`
- "verifica minha implementação com o codex" → `verify`
- "delega ao codex" → `delegate` (exige confirmação explícita)
- "faz um insight da sessão" → `insight`

## Estrutura

```
.
├── SKILL.md                       # entrada da skill (modos, exemplos, regras)
├── SETUP.md                       # requisitos, env vars, instalação
├── ROADMAP.md                     # itens em aberto (não implementados ainda)
├── README.md                      # este arquivo
├── LICENSE                        # MIT
├── scripts/
│   ├── invoke_codex_with_claude.py  # wrapper canônico
│   ├── invoke_codex_with_claude.ps1 # shim PowerShell
│   ├── codex_batch.py               # batching paralelo (modos batch-*)
│   ├── build_review_packet.py       # monta packet para plan-review
│   ├── collect_claude_context.py    # coleta CLAUDE.md global/repo/target
│   ├── dump_transcript_for_codex.py # dump filtrado do transcript
│   ├── normalize_codex_result.py    # normaliza saída crua do Codex
│   └── codex_output_schema.json     # JSON Schema (--output-schema)
└── tests/
    ├── test_codex_skill.py          # testes mockados (rápidos)
    ├── test_codex_live.py           # testes contra Codex real (gastam tokens)
    └── fake_codex.py                # Codex falso parametrizável
```

## Segurança

- **`delegate` roda com `--sandbox danger-full-access`** — o Codex pode
  criar, editar ou deletar arquivos em qualquer lugar do disco. A skill
  exige confirmação explícita do usuário antes de cada chamada, listando
  tarefa literal, `cwd`, branch, paths fora do workspace e palavras de
  risco detectadas (`delete`, `rm -rf`, `force`, `reset --hard`, etc.).
- Todos os outros modos usam `--sandbox read-only`.
- Credenciais propagadas para o subprocesso são declarativas em
  `settings.credentials.propagate` (config-driven, default vazio). As
  fontes ficam em `settings.credentials.source` (lista ordenada,
  default `["./.env", "~/.claude/credentials.env"]`). Apenas as chaves
  listadas são injetadas no env do subprocesso a partir desses
  arquivos — valores nunca aparecem em logs. Variáveis já presentes no
  env do processo pai são herdadas como em qualquer subprocesso POSIX.
- Telemetria local (`cache/runs.jsonl`) está no `.gitignore` por padrão.

## Status e roadmap

A skill está em uso real. Itens em aberto (não implementados ainda) estão
listados em [`ROADMAP.md`](ROADMAP.md):

- Background agents nomeados (execução não-bloqueante).
- Conversa multi-jogada Claude ↔ Codex (até 5 turnos).
- Cache de revisões por fingerprint, modo repro, parser de stream `--json`.

## Licença

[MIT](LICENSE).
