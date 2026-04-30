# Roadmap — skill `codex`

Itens registrados como evolução futura. Não implementar até que o critério
de início esteja claro.

## 1. Background agents (não-bloqueantes)

Hoje todo modo do wrapper é síncrono — Claude espera o resultado. Para
runs longos isso bloqueia a sessão.

Forma esperada:

- `codex_bg.py start` aceita os mesmos args de um modo do wrapper e
  devolve um `run_id` curto.
- `codex_bg.py status <run_id>` retorna `running | done | error | cancelled`
  com ETA.
- `codex_bg.py output <run_id>` retorna o JSON canônico quando `done`.
- `codex_bg.py cancel <run_id>` mata o subprocesso e marca `cancelled`.
- `codex_bg.py list` lista runs ativos e recentes (últimos N).
- Estado persistido em `cache/bg_runs/<run_id>/` com `meta.json`,
  `output.json`, `stderr.log`. Cleanup de runs órfãos por mtime.
- Heartbeat continua indo para um arquivo de log do próprio run, não para
  o stderr da sessão original.

Critério para iniciar:

- Batch síncrono usado em casos reais (telemetria mostrando volume).
- Telemetria evidenciando que o bloqueio em sessão é dor recorrente.
- UX de status/cancelamento desenhada — como Claude apresenta o `run_id`,
  como retoma, como o usuário recupera output esquecido.

## 2. Conversa multi-jogada Claude ↔ Codex (até 5 turnos)

Discussão iterativa em vez de revisão one-shot:

- Turno 1: Claude envia plano → Codex revisa, devolve findings.
- Turno 2: Claude atualiza o plano em resposta aos findings → Codex
  revisa de novo.
- Repete até `max_turns` (default 5) ou até Codex devolver
  `findings=[]` + `severity=low` por dois turnos consecutivos.

Mecânica:

- Estado da conversa em `$env:TEMP/codex_dialogue_<id>/turn_<n>.json`
  com plano + findings + decisão de Claude por turno.
- Cada chamada subsequente ao Codex inclui:
  - plano atualizado;
  - histórico das jogadas anteriores (resumo curto: turno N → findings
    aceitos/rejeitados/o que mudou);
  - foco explícito ("revise apenas as mudanças desde o turno N-1").
- Ferramenta de saída humana: o usuário pode interromper a qualquer turno
  com "para aqui" e Claude apresenta o plano final.
- Critério natural de parada: convergência (Codex sem findings novos),
  divergência (Claude e Codex discordam fundamentalmente — escala ao
  usuário), ou limite de turnos.
- Output final: plano consolidado + log enxuto da conversa (não mostra
  cada turno verbatim, mostra o diff entre turnos).

Critério para iniciar:

- `plan-review` simples já está em uso real e estabilizado.
- Telemetria mostra casos onde o usuário pediu re-revisão manualmente
  após ajustar o plano (sinal de demanda).
- Definir como apresentar a conversa final ao usuário sem virar parede de
  texto — o valor está no plano consolidado, não no histórico.

## 3. Profissionalização do repo público

O repo está público em https://github.com/lucas-lima-s/claude-codex-skill,
mas só com o mínimo (LICENSE MIT, README pt-BR, `.gitignore`). Itens abaixo
levam o repo de "código pessoal jogado no GitHub" para "skill instalável
por outro usuário sem precisar perguntar nada".

### Estado atual

- ✅ LICENSE (MIT), README.md (pt-BR), `.gitignore`, repo público.
- ❌ CI, CHANGELOG, CONTRIBUTING, README/SETUP em inglês.
- ❌ Issue/PR templates, topics, descrição em inglês no GitHub.
- ❌ `.gitattributes` para normalizar line endings.
- ❌ Auditoria de hardcoded paths e mistura pt-BR/inglês fora dos pontos
  autorizados pelo `CLAUDE.md`.

### Tooling e qualidade

- **CI no GitHub Actions:** lint (ruff/black/mypy se aplicável) + suite
  mockada (`tests/test_codex_skill.py`) cross-platform (`ubuntu-latest`
  e `windows-latest`) em push e PR. **Excluir** `test_codex_live.py`
  do CI (gasta tokens contra o Codex real) ou usar `FAKE_CODEX_BEHAVIOR`
  para mockar.
- **Issue templates + PR template:** versões básicas
  (`.github/ISSUE_TEMPLATE/bug_report.md`, `feature_request.md`,
  `.github/PULL_REQUEST_TEMPLATE.md`).
- **`.gitattributes`** com `* text=auto eol=lf` (ou similar) para
  evitar confusão de CRLF/LF entre contribuintes Windows e Linux.

### Documentação e identidade

- **README em inglês** como primário (alcance público maior); README
  pt-BR vira `README.pt-BR.md` ou seção secundária. Inclui demo/screenshot
  de um `plan-review` real renderizado em markdown.
- **SETUP em inglês** (`SETUP.md` em inglês + `SETUP.pt-BR.md`).
- **ROADMAP traduzido** para inglês.
- **CHANGELOG.md retroativo** cobrindo as fases anteriores
  (Phase 0–3 do desenvolvimento da skill).
- **CONTRIBUTING.md** com setup local, como rodar testes mockados,
  como adicionar um novo modo, política de commits.
- **Topics no GitHub:** `claude-code`, `codex`, `skill`, `cli`,
  `automation`, `python`.
- **Descrição do repo (1 linha em inglês):**
  *"Claude Code skill that delegates plan reviews, verifications, and
  tasks to OpenAI Codex CLI."*

### Critério de aceitação

- Outro usuário (não-Lucas) consegue clonar, configurar `$SKILLS_PYTHON`,
  e rodar a suite de testes em <10 min seguindo só os docs.
- CI verde em ambas as plataformas para a suite mockada.
- Nenhum path hardcoded para a máquina pessoal.
- Nenhuma string em pt-BR fora dos pontos onde a regra do `CLAUDE.md`
  autoriza (description da skill, instruções de trigger pt-BR no
  `SKILL.md`, mensagens user-facing intencionais).

### Ordem sugerida

1. Auditoria de hardcoded paths e strings pt-BR/inglês (Grep cego no repo).
2. `.gitattributes` (antes de qualquer outra mudança em arquivos).
3. README.md em inglês (mais alto impacto público).
4. SETUP.md em inglês (ou criar `SETUP.en.md`).
5. ROADMAP.md traduzido.
6. CHANGELOG.md retroativo (Phase 0–3).
7. CONTRIBUTING.md.
8. GitHub Actions workflow (lint + suite mockada).
9. Issue templates + PR template.
10. Topics + descrição do repo (ação manual no GitHub).

### Decisões pendentes

- README primário em inglês com pt-BR secundário, ou manter pt-BR primário
  e adicionar versão em inglês?
- Strings user-facing dos scripts (errors, prints) — manter pt-BR,
  adicionar i18n simples, ou converter para inglês? A regra atual do
  `CLAUDE.md` permite pt-BR para texto que o usuário vê no terminal.
- CI agora ou só após estabilizar itens 1 e 2 deste roadmap?
- Aceitar contribuições externas via PR ou manter como repo "read-only
  para o público, write-only para o autor"?

## 4. (Aberto) Outros itens

- Streaming de eventos `codex exec --json` com parser robusto (hoje a flag
  é opt-in mas o output é tratado como raw — beneficia heartbeat e parse
  parcial).
- Cache de revisões por fingerprint do packet — evita re-revisar plano
  idêntico já visto.
- Modo `repro` que dado um `run_id` regrava prompt + output em pasta
  legível pra debug humano.
