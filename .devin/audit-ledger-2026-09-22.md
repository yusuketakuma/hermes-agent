# Repo-wide Audit Ledger — 2026-09-22

- RUN ID: audit-2026-09-22-devin
- Repo: /Users/yusuke/.hermes/hermes-agent (hermes-agent)
- B (baseline): HEAD 1a90fad0f5 on main (== origin/main)
- Start state: tracked tree clean; 73 pre-existing untracked items (user's personal skills/, .claude/.devin/.omc/.omo/.opencode) — preserved, excluded from scope.
- NOTE: live Hermes gateway runs from this checkout (~/.hermes/state.db, gateway.pid). Real user state — no destructive ops, no commits/stage per instructions.
- Runtime: python .venv (pytest 9.1.1, ruff 0.15.10). Verification entry: `scripts/run_tests.sh`, `ruff check`, pytest addopts `-m 'not integration'`.

## Coverage matrix (C01–C12, X01–X08)
State: REVIEWED / PARTIAL / NOT_APPLICABLE + evidence.

| Area | State | Evidence |
|---|---|---|
| C01 設計 | PARTIAL | facade+siblings/bind_module architecture confirmed via AGENTS.md + source |
| C02 業務正確性 | PARTIAL | naive/aware datetime mixing audited (68 naive vs 110 aware call-sites — naive uses are all strftime-only, no arithmetic boundary violation found); `x = list.sort()`-style sink check clean |
| C03 永続化 | PARTIAL | all f-string SQL sites enumerated — interpolation limited to internal column/placeholder constants |
| C05 security | PARTIAL | every non-test `shell=True` site reviewed (manifest-defined bootstrap, user-typed bang/gate/editor commands — intentional trust model); no bare `except:` in source; scoped-secret rules verified in gateway/AGENTS.md |
| C10 信頼性 | PARTIAL | B023 loop-closures reviewed — all immediate-invocation; RUF006 dangling tasks: 24 sites, consistent intentional fire-and-forget, no confirmed failure path |
| C11 テスト | REVIEWED (変更近傍) | 19/19 router efforts tests pass; 81+1 skip across touched-area suites |
| C12 build/tooling | REVIEWED | `ruff check` (project config) clean; ty full-repo check OOM-killed (tooling limit, noted) |
| C04,C06,C07,C08,C09 | PARTIAL | not exhaustively audited — repo scale (~39k tests) exceeds one-pass coverage; targeted static analysis (F/B/S/PIE/RET/RUF/W605/E7 families) applied repo-wide |
| X01–X08 | NOT_APPLICABLE/PARTIAL | X02 partially (no new deps added); X06 partially (delegation/agent rules read); others not triggered by findings |

## Findings
| ID | class | P | path/symbol | condition | status |
|---|---|---|---|---|---|
| F1 | CONFIRMED_BUG | P2 | plugins/model-providers/router/__init__.py: `_DISK_TTL_SECONDS` | commit 208bd0b6 removed the constant; 2 uses survived → NameError in `_efforts_cache_only` whenever a readable disk mirror exists (effort clamp silently returns None for that call; stale-mirror warm kick never runs) and in `_load_disk` on unparseable `ts` (valid efforts map dropped). Reproduced: `TestDiskMirror` fails with NameError pre-fix. | FIXED |
| F2 | EVIDENCE_BASED_CONCERN | P3 | hermes_cli/cli_init_mixin.py: `_present_store_warning` closure reads except-bound `e` (F821) | `except … as e` names are unbound after the block; synchronous `render_notification` call works today, but any deferred invocation raises NameError and silently drops the Details line | FIXED |
| F3 | OPTIONAL_IMPROVEMENT | P3 | tui_gateway/session_compression.py: dead `model_cfg` | assigned, never read (cfg read directly downstream) | FIXED |
| F4 | CONFIRMED_BUG | P3 | hermes_cli/uninstall.py `_hermes_path_markers` docstring | `git\bin`/`<root>\bin` — `\b` is a real escape → docstring contains a literal backspace; `git\cmd` invalid escape warns under -W. Runtime doc corrupted. | FIXED (r""" """) |
| C1 | EVIDENCE_BASED_CONCERN | P3 | RUF006 dangling create_task ×24 | no reproduction; consistent intentional pattern — NOT fixed (no confirmed defect; per-audit rules forbid best-practice-only churn) | OPEN |
| C2 | EVIDENCE_BASED_CONCERN | P3 | RUF012 mutable class attrs ×105 | spot-checks are intentional class constants / plugin-compat; not exhaustively verified | OPEN |
| C3 | EVIDENCE_BASED_CONCERN | P3 | agent/codex_runtime.py:1182 `intercepted_events` loop-closure | finalizer reads list by reference; rebind-on-retry semantics unverified — no confirmed failure | OPEN |

## Repair units
| ID | FIX/REFACTOR | finding | scope | verification |
|---|---|---|---|---|
| R1 | FIX | F1 | restore `_DISK_TTL_SECONDS = 24*60*60` + comment | 4 new regression tests in TestDiskMirror; file suite 19/19 |
| R2 | FIX | F2 | default-arg bind `exc=e` in closure | compile + test_cli_init suite |
| R3 | FIX | F3 | delete dead `model_cfg` line | test_compression_config_hot_reload suite |
| R4 | FIX | F4 | docstring → raw string | repr() shows literal backslashes |

## Verification log
| when | command | scope | result |
|---|---|---|---|
| 2026-09-22 | `ruff check .` | repo | clean (project rules) |
| 2026-09-22 | `ruff --isolated --select F/B/S/PIE/RET/RUF/E7/W605` | all source dirs | F821/F841 triaged → findings above |
| 2026-09-22 | pytest test_router_codex_efforts.py | R1 | 19 passed (4 new regression tests) — pre-fix run reproduced NameError |
| 2026-09-22 | pytest test_cli_init, test_uninstall_*, test_compression_config_hot_reload | R2–R4 | 81 passed, 1 skipped |
| 2026-09-22 | pytest tests/agent/transports/ (whole dir) | R1 vicinity | 438 passed |
| 2026-09-22 | pytest multiplex_plugin_cache_scope + test_model_catalog + touched suites | final gate | 111 passed, 1 skipped |

## Independent review (subagent_explore, fresh context)
- Result: CLEAN on all 5 files. Verified restored constant matches `hermes_cli/models_reasoning_caps.py` sibling template (86400s, same stale-serve+warm semantics); confirmed all 4 new tests fail pre-fix (NameError) and pass post-fix; confirmed no module-state leakage between tests; confirmed `model_cfg` dead (no dynamic access); confirmed raw docstring safe.
- Nit fixed: test-file docstring updated (in-memory → disk path mention).
- Not verified by reviewer (no exec tool): `git show` of the original commit, live pytest run — covered by parent-side evidence (git log -p -S showed the deleted `_DISK_TTL_SECONDS = 24 * 60 * 60`; pytest runs above).

## Remaining (documented, not blockers)
- C1/C2/C3 concerns in Findings (no confirmed defect — left OPEN with evidence, per audit rules).
- Repo-scale exhaustive coverage is PARTIAL by construction (~39k tests, ~500k+ lines); recorded honestly rather than fabricating REVIEWED.

## Delivery
- Cumulative diff: 5 modified files (+70/-5), uncommitted per instructions. New file: .devin/audit-ledger-2026-09-22.md (this ledger). No commits/stage/push. Pre-existing untracked user data preserved untouched.
- Checkpoint: this ledger + `git diff` reproduces the change; baseline B = 1a90fad0f5.

