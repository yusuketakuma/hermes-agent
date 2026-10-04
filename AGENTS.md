# Hermes Agent — Engineering Contract

Hermes shares an agent core across CLI, gateway, TUI, dashboard, and desktop.
Capability belongs at the edges; keep the core and model-tool schema narrow.

This file is the startup contract. Read the relevant complete section of
[AGENT_GUIDE.md](AGENT_GUIDE.md) when changing that subsystem; do not preload the
whole guide. Source and tests establish current behavior. Preserve existing and
concurrent changes; do not reset, restore, stash, clean, or revert others' work.

## Required invariants

- Fix the verified cause and sibling call paths. Trace the live path and the
  original design before treating an intentional restriction as a gap. Security
  fixes must preserve the feature's purpose. Declared refactors may be large;
  footprint restraint must not remove requested product functionality.
- Preserve conversation prompt caching: past context, toolsets, and system
  prompts stay byte-stable until context compression. No mid-conversation memory
  reloads. Strict message role alternation: no consecutive same-role messages or
  synthetic user messages mid-loop. State-changing slash commands defer prompt
  invalidation to the next session; immediate invalidation requires opt-in
  `--now` (see `/skills install --now`).
- Reuse infrastructure before adding it. New capability: existing code → CLI +
  skill → service-gated tool (`check_fn`) → plugin → catalog MCP → core tool.
  A generic interface needs concrete consumers, not speculative hooks. When
  several integrations share a category, use one provider interface/orchestrator.
- Plugins own their directories and use generic ABCs/hooks; do not special-case
  a plugin in core. Third-party vendor products ship as standalone plugin repos,
  not bundled directories. Do not add telemetry or attribution without explicit
  user opt-in through config, setup, and `hermes tools`.
- Tools that load required instructions must return the complete content; no
  offset/limit escape hatch for skills, prompts, or playbooks. Tool descriptions
  must not name unavailable tools from other toolsets; dynamic cross-references
  belong in `get_tool_definitions()` in `model_tools.py`.
- Add behavioral settings to `DEFAULT_CONFIG` in `hermes_cli/config.py` and expose
  them through existing setup/config UX. `.env` is for credentials only.
  `_config_version` changes only for a migration/transform, not an additive key.
  Verify CLI `load_cli_config()`, CLI `load_config()`, and raw gateway YAML paths
  when settings propagate across surfaces. Messaging cwd is `terminal.cwd`;
  CLI cwd is the process directory. Do not revive `MESSAGING_CWD`.
- State paths use `get_hermes_home()`; displayed paths use
  `display_hermes_home()` from `hermes_constants`. Profile selection precedes
  imports. Profile enumeration/default-home operations intentionally remain
  `Path.home()` anchored; do not couple isolated profile configurations.
  Credential-bearing adapters acquire/release `gateway.status` scoped locks.
- Gateway approval/control commands must bypass both the base adapter's active
  session queue and runner interception, and dispatch inline. Check both guards
  for `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny` changes.
- Use `hermes_cli/curses_ui.py` for new menus, not `simple_term_menu`. Avoid ANSI
  erase-to-EOL in spinner/display code; use space padding. Child execution saves
  and restores `model_tools._last_resolved_tool_names`; account for its temporary
  process-global state.
- Preserve contributor authorship when salvaging work. Before authorized merges,
  inspect branch/main ancestry and unrelated deletions; do not reset a populated
  worktree to update a stale branch. Publishing and external actions require the
  applicable session/user authorization; a rubric is not permission.

## Dependencies and validation

- PyPI dependencies require a floor and ceiling (`>=current,<next_major`; for
  pre-1.0 use the guide's minor ceiling). Git dependencies and GitHub Actions use
  commit SHAs; CI-only pip installs use exact versions. Regenerate `uv.lock`
  with `uv lock` after dependency changes.
- **Always use `scripts/run_tests.sh`, never direct pytest.** It isolates
  credentials, HOME/HERMES_HOME, locale/timezone, and test-file subprocesses.
  Select the affected files/directories first; use the full suite when required.
  Do not use xdist to replace the runner's per-file isolation.
- Tests use temporary state, never the real `~/.hermes`. Profile tests mock
  `Path.home()` and set `HERMES_HOME` together. Test configuration/resolution,
  security, backend, and file/network seams through real imports against a temp
  home; unit mocks alone do not prove the integrated path. Never activate dead
  code without that validation.
- Assert behavioral relationships/invariants, not changing catalogs, version
  literals, enumeration counts, source text, or AST implementation details.
  Exercise real objects and observable behavior. Tests of JS artifacts or
  configuration belong in their vitest suite, so CI change classification runs
  them; do not put them in Python tests.
- Pass-on-retry is reported as `FLAKY`, not ignored. Preserve both attempts'
  failure output and fix timing assumptions with event-based sync and realistic
  bounds. Do not weaken tests to obtain green results.

```bash
scripts/run_tests.sh tests/gateway/
scripts/run_tests.sh tests/agent/test_foo.py -k test_x
scripts/run_tests.sh                  # full suite when required
```

## Where to read next

| Change | Entry points and guide sections |
|---|---|
| Agent/provider/tool loop | `run_agent.py`, `agent/`, `model_tools.py`, `toolsets.py`; File Dependency Chain, AIAgent Class, Adding New Tools, Toolsets |
| CLI/slash commands/config | `cli.py`, `hermes_cli/`; CLI Architecture, Adding Configuration, Skin/Theme System |
| Gateway/platforms/profiles | `gateway/`, `hermes_constants.py`, `ADDING_A_PLATFORM.md`; Important Policies, Profiles, Known Pitfalls |
| TUI/dashboard/desktop | `ui-tui/`, `tui_gateway/`, `apps/desktop/`; TUI Architecture. For desktop also read its nested `AGENTS.md` and `DESIGN.md` |
| Plugins/skills/delegation | `plugins/`, `tools/delegate_tool.py`, `skills/`, `optional-skills/`; Plugins, Skills (including HARDLINE authoring standards), Delegation, Curator |
| Scheduled/queued work | `cron/`, `plugins/kanban/`; Cron, Kanban. Process-local background delegation does not survive restart |
| Tests/dependencies | `tests/`, `scripts/run_tests.sh`, `pyproject.toml`; Dependency Pinning Policy, Testing |
| Contribution review/triage | Contribution Rubric including the three permitted automated closure reasons; taste/scope decisions stay with a human |

The guide preserves the detailed subsystem contracts and examples. Read the
relevant section fully, then inspect actual callers and tests. Existing scoped
local authorization covers implementation and necessary validation; do not
start deployment, messaging, or unrelated maintenance from a guide example.

## Semantic code search (jevgrep)

- 実装箇所・関係・影響が未確定で完全一致検索では探しにくいときは `jg` を使う。既知ファイル・正確なシンボル・局所修正は read/rg/LSP でよい。
- ユーザーの継続承認（2026-09-28）は、全リポジトリのコード探索・理解・動作確認のため、非公開コードと質問を設定済み provider と Jevgrep 処理サービスへ送る範囲。一般的な事前承認はこの範囲で充足済み。
- PHI/PII・認証情報・secret・本番/実データ・送信権限のない第三者資料は除外する。混在時は安全なサブツリーか確認済みファイルの一時コピーを使う。拡張フラグも安全確認後だけ。ほかの外部送信・公開・push/PR/merge/deploy・契約/課金変更の承認にはならない。
- 全体調査は主要領域を横断するか領域ごとに検索し、候補を実ファイルで確認する。利用不能なら理由を示しローカル探索を続け、重要な未探索範囲・不完全さを報告する。検索結果は網羅性やテスト成功の証明ではない。

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Do not enumerate, preload, or search wikis at task start. Use retrieval when the user asks for it, when unfamiliar architecture or dependency behavior materially affects the task, or when source inspection leaves an important uncertainty. Stop once the question is grounded.
- When those conditions apply and OpenWiki retrieval tools are available, use `openwiki_search` for just-in-time context and `openwiki_read` for the relevant complete sections. If search returns `workspace_required`, ask which listed workspace to use and retry with its ID.
- Use `openwiki_list_workspaces` or `openwiki_list_wikis` when workspace membership itself needs to be discovered.
- If the retrieval tools are unavailable, read `openwiki/quickstart.md` and follow its links to the relevant pages.
- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
