---
name: cheap-llm
description: Offload drafting, bulk reading, summarising, translation and boilerplate code to cheap LLMs (MiniMax M3.1 Flash, GLM 5.3 Flash) through the agge CLI to save Claude tokens. Use before any task that would make Claude read many files or write long text/code itself.
---
# Offload work to cheap LLMs

Claude's tokens are expensive. The user provides `MINIMAX_API_KEY` and `NVIDIA_API_KEY`
for cheaper models. Let them do the bulk work; Claude plans, reviews and verifies.

## Models

| alias | model | key | notes |
|---|---|---|---|
| `minimax` | MiniMax-M3.1-Flash (api.minimaxi.com) | `MINIMAX_API_KEY` | ~2 s, default first choice |
| `glm` | nvidia:z-ai/glm-5.3-flash | `NVIDIA_API_KEY` | free endpoint, often ~80 s queue |

Always pass `-m minimax,glm` so a failure falls back to the next model.

## What to offload / keep

Offload: reading and summarising many files, first drafts of docs/chapters, translation,
per-file batch transforms, boilerplate modules from a precise spec, exploring unfamiliar code.

Keep for Claude: architecture decisions, tricky debugging, final review, anything that needs
the conversation's context, short edits (offloading a 3-line fix costs more than doing it).

## Commands (work in any directory, no `agge init` needed)

```bash
# one question, only the final answer, attach files, no tools
agge ask -q --no-tools -m minimax,glm -f 'src/*.py' --brief 10 "summarise the architecture"

# let the agent read/write files itself; JSON tells you which files changed
agge ask -y --json -m minimax,glm "implement X per SPEC.md, run the tests"

# long result straight to a file, print one line
agge ask -y --save out/report.md -m minimax,glm "..."

# same prompt over many files in parallel
agge map -m minimax,glm --brief 1 --json "one-line purpose of this file" 'src/**/*.py' -j 8
agge map -m minimax,glm -d en --ext .md "translate {{file}} to English" 'chapters/*.md'

# bigger workflows
agge code -y "requirement" -o dir -m minimax,glm
agge book -y "topic" -o dir --chapters 10 --lang 繁體中文
```

Full reference: `doc/offload.md` in the agge repo (`/Users/Shared/ccc/project/agge`).

## Rules that keep it cheap and correct

1. Never read the cheap model's full output into context if avoidable: use `--brief N`,
   `--save FILE`, `--json` (`files_changed`), then `tail` / `grep` / `git diff --stat`.
2. For code, write a precise spec file and pass it with `-f`; one module per call.
3. Always run and test what came back. Known failure modes: truncated output, stray garbage
   lines (e.g. `]<]minimax[>[`), empty replies — retry once, then fall back or do it yourself.
4. Long jobs (code modules take minutes): run in the background (`run_in_background`) and do
   integration work meanwhile.
5. Tell the user what was offloaded and what you verified yourself.
