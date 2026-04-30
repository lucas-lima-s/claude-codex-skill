## Summary

What does this PR change and why?

## Checklist

- [ ] Tests pass: `python tests/test_codex_skill.py`
- [ ] Lint pass: `ruff check .` and `black --check .`
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] If a new mode: SKILL.md, config.default.json (both pt-BR and
      en-US bundles), tests/fake_codex.py, and a test case in
      tests/test_codex_skill.py are all updated
- [ ] No hardcoded paths (only env vars: `SKILLS_PYTHON`, `CODEX_*`)
- [ ] No pt-BR strings outside `SKILL.md`'s description/triggers and
      `config.default.json`'s `pt-BR` section
