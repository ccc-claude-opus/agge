# agge — Agent Graph Engineering

用 git 風格的瑞士刀指令 `agge <op> <args...>` 組織 LLM agent 團隊寫程式、寫書。
可以像 codex / opencode 一樣直接對話,也可以把多個 agent 編排成 **graph** 平行執行。

- 純 Python 標準函式庫,無相依套件(Python ≥ 3.10)
- 多模型:每個 agent、每個 graph 節點都可以指定不同模型
- Provider:`nvidia`、`openai`、`anthropic`、`openrouter`、`deepseek`、`groq`、`gemini`、`ollama`、`lmstudio`、任意 OpenAI 相容端點、`echo`(離線測試)
- 工具採文字標籤協定(`<write_file>`、`<shell>`…),任何模型都能用,不需要 function calling

## 安裝

```bash
pip install -e .
```

## 快速開始

```bash
export NVIDIA_API_KEY=nvapi-...
agge init                 # 建立 .agge/,有 NVIDIA key 時預設模型為 glm (z-ai/glm-5.3-flash)
agge                      # 進入互動對話(= agge chat)
```

## 兩種組織團隊的方式

**1. 動態:團隊模式對話(lead 分派任務)**

```bash
agge chat --team code     # 與 lead 對話;lead 會用 <delegate> 把子任務派給 architect/coder/tester/reviewer
```

同一則回覆中的多個 `<delegate>` 會平行執行。對話中也可以 `@coder 修這個 bug` 直接找某個成員。

**2. 靜態:Graph 工作流程**

```bash
agge code "寫一個 Markdown 轉 HTML 的 CLI,含測試" -o md2html --stack python
agge book "EDA 高階合成 (HLS) 的歷史與現況" -o hlsBook --chapters 8 --lang 繁體中文
agge run brainstorm "如何設計快取層"
```

內建 graph:

| graph | 流程 |
|---|---|
| `solo` | assistant 單獨完成 |
| `code` | design → implement → test → review,審查結果含 `VERDICT: REVISE` 時回到 implement(最多 2 輪) |
| `book` | outline → chapters(依大綱的 JSON 章節清單**平行**撰寫每章)→ edit |
| `brainstorm` | 三個 agent 平行提案 → lead 綜合 |

`-n/--dry-run` 只顯示流程與模型分配;`-o DIR` 指定輸出目錄;`-y` 自動核准 shell。

## 指定模型

```bash
agge model list
agge model add claude anthropic claude-sonnet-5-5
agge model add ds deepseek deepseek-chat --temperature 0.2
agge model add local ollama qwen3:8b
agge model add myapi openai my-model --base-url http://host:8000/v1 --api-key-env MY_KEY
agge model default glm
agge model test claude
```

選擇優先順序(高 → 低):`-M agent=模型` 或 `-M 節點=模型` > graph 節點的 `model` > agent 設定的 `model` > `-m 模型` > `default_model`。
也可以直接寫 `provider:model`,例如 `-m nvidia:z-ai/glm-5.3-flash`。

```bash
agge code "..." -m glm -M review=claude -M implement=ds
```

對話中用 `/model claude` 切換目前 agent 的模型。

## 自訂 graph

```bash
agge graph new mycode --from code
agge graph add-node mycode docs --agent writer --after implement --prompt "為 {{implement}} 寫 README"
agge graph add-edge mycode test implement --when "RESULT: FAIL" --max 3
agge graph show mycode
agge graph mermaid mycode
agge run mycode "任務"
```

Graph 檔(`.agge/graphs/<name>.json`):

```json
{
  "nodes": {
    "outline":  {"agent": "outliner", "prompt": "Plan: {{task}}"},
    "chapters": {"agent": "writer", "map": "outline", "prompt": "Write {{item.title}} to {{item.file}}"},
    "edit":     {"agent": "editor", "model": "claude"}
  },
  "edges": [["outline", "chapters"], ["chapters", "edit"],
            {"from": "edit", "to": "chapters", "when": "REWRITE", "max": 1}]
}
```

- 樣板變數:`{{task}}`、`{{節點名}}`、`{{deps}}`(所有上游輸出)、`{{feedback}}`(迴圈回饋)、`{{item}}`/`{{item.欄位}}`/`{{index}}`
- 相依已完成的節點會**平行**執行;`map` 節點對來源輸出中的清單(```json 陣列或條列)每項各跑一次
- 帶 `when`(正規表示式)的邊是條件迴圈:命中時重跑目標與其下游,最多 `max` 次

## 自訂 agent 與團隊

```bash
agge agent add translator --description "中英翻譯" --system "You translate technical text..." \
     --tools read_file,write_file --model claude
agge team add docs --lead lead --members outliner writer translator editor
agge chat --team docs
```

工具:`read_file`、`write_file`、`edit_file`、`list_files`、`search`、`shell`(需核准)、`delegate`。

## 對話指令

```
/agent coder   /agents   @reviewer 看一下 x.py   /team code   /team off
/model claude  /models   /run book <主題>       /graphs   /graph code
/add-dir ~/books   /dirs   /yes   /compact   /clear   /new   /sessions   /resume
/save out.md   /exit          多行輸入用 """ 包起來
```

## 其他指令

```bash
agge status                 # 專案概況
agge log                    # 執行紀錄;agge log last review 看某節點的 prompt 與輸出
agge session list|show
agge dir add ~/books        # 允許 agent 存取專案外的目錄
agge config approval auto   # shell 核准模式 ask|auto|deny
agge config language 繁體中文
agge provider               # 列出 provider 與 API key 狀態
```

## 專案結構

```
agge/
  cli.py        git 風格子指令
  repl.py       互動對話
  runtime.py    Agent 迴圈、模型選擇、delegate
  graph.py      graph 驗證/描述/mermaid 與平行執行器(map、條件迴圈)
  tools.py      文字標籤工具、工作區沙盒、shell 核准
  providers.py  LLM provider(NVIDIA/OpenAI 相容、Anthropic、echo)
  builtins.py   內建 agents/teams/graphs
  project.py    .agge/ 狀態:config、sessions、runs
```

使用者層級設定放在 `~/.agge/config.json`(例如共用的 models),會被專案設定覆蓋。
