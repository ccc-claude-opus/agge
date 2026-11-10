#!/usr/bin/env bash
# agge test script
#
#   ./test.sh            離線測試:pytest + CLI 煙霧測試(echo 模型,不需 API key)
#   ./test.sh live       再加上真實模型的快速測試:對話記憶、slash 指令、工具呼叫、團隊 delegate
#   ./test.sh all        再加上完整工作流程:agge code(寫程式並跑測試)、agge book(寫 3 章小書)
#
# 環境變數:
#   AGGE_TEST_MODEL   使用的模型(預設 minimax = MiniMax-M3.1-Flash;也可用 glm = nvidia:z-ai/glm-5.3-flash)
#   AGGE_TEST_DIR     測試工作目錄(預設在暫存目錄建立,結束後保留以便檢查)
#
# 注意:glm 走 NVIDIA 免費端點,每次呼叫可能排隊 1 分鐘以上,all 模式可能 30 分鐘以上。

set -u
MODE="${1:-offline}"
REPO="$(cd "$(dirname "$0")" && pwd)"
MODEL="${AGGE_TEST_MODEL:-minimax}"
WORK="${AGGE_TEST_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/agge-test.XXXXXX")}"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export NO_COLOR=1
export AGGE_HOME="$WORK/home"      # 不讀取使用者的 ~/.agge 設定

PASS=0
FAIL=0
FAILED=()

agge() { python3 -m agge "$@"; }
section() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok() { PASS=$((PASS + 1)); printf '  \033[32m✔ %s\033[0m\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); FAILED+=("$1"); printf '  \033[31m✘ %s\033[0m\n' "$1"; }
check() {  # check "描述" command...
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
contains() {  # contains "描述" file pattern
    if grep -qE -- "$3" "$2" 2>/dev/null; then ok "$1"; else bad "$1  (在 $2 找不到 /$3/)"; fi
}
newproject() {  # newproject name model -> cd into a fresh agge project
    mkdir -p "$WORK/$1" && cd "$WORK/$1" && agge init -m "$2" >/dev/null
}

echo "agge 測試  模式=$MODE  模型=$MODEL"
echo "工作目錄: $WORK"

# ------------------------------------------------------------------ offline
section "pytest"
if (cd "$REPO" && python3 -m pytest -q tests); then ok "pytest"; else bad "pytest"; fi

section "CLI 煙霧測試 (echo)"
newproject offline echo
check "agge status" agge status
check "agge provider" agge provider
check "agge graph list" agge graph list
for g in solo code book brainstorm; do check "graph validate $g" agge graph validate "$g"; done
agge run solo "hello world" > run.txt 2>&1
contains "run solo 回傳 echo" run.txt "\[echo\].*hello world"
check "graph new --from code" agge graph new mine --from code
check "graph add-node" agge graph add-node mine docs --agent writer --after implement
check "graph add-edge (條件迴圈)" agge graph add-edge mine test implement --when "RESULT: FAIL" --max 2
check "graph validate mine" agge graph validate mine
agge graph mermaid mine > mermaid.txt
contains "mermaid 輸出" mermaid.txt "test -\. \"RESULT: FAIL"
agge code -n "x" -M review=nvidia:z-ai/glm-5.3-flash > dry.txt 2>&1
contains "dry-run 顯示 -M 指定的模型" dry.txt "review +→ nvidia:z-ai/glm-5.3-flash"
check "agent add" agge agent add translator --base writer --description "翻譯" --model echo
check "team add" agge team add docs --members outliner translator editor
agge log > log.txt
contains "agge log 有紀錄" log.txt "solo +done"
printf '/agents\n/model echo\n@coder ping\n/team code\n/yes\nhi\n/exit\nnever-sent\n' | agge chat > chat.txt 2>&1
contains "chat: /agents" chat.txt "translator"
contains "chat: @coder" chat.txt "\[echo\] ping"
contains "chat: /team code 切到 lead" chat.txt "對話對象: lead"
if grep -q "never-sent" chat.txt; then bad "chat: /exit 後不應繼續"; else ok "chat: /exit 停止"; fi
printf '<write_file path="w.txt">hi</write_file>\n' | agge chat > tool.txt 2>&1
check "chat: echo 回傳的工具標籤會被執行" test "$(cat w.txt 2>/dev/null)" = "hi"

# ------------------------------------------------------------------ live
if [[ "$MODE" == "live" || "$MODE" == "all" ]]; then
    section "真實模型: $MODEL"
    newproject live "$MODEL"
    if agge model test "$MODEL" --prompt "只回答數字:3+4=?" > model.txt 2>&1; then ok "model test"; else bad "model test"; fi
    contains "model test 回答 7" model.txt "7"

    printf '我的名字是小明，請記住。只回答「好」。\n我叫什麼名字？只回答名字。\n' | agge chat > memory.txt 2>&1
    contains "對話記憶 (多輪)" memory.txt "小明"

    agge ask -y "建立 hello.py,內容為印出 Hello agge 的程式,然後用 shell 執行 python3 hello.py 確認輸出" > ask.txt 2>&1
    contains "ask: 使用 write_file" ask.txt "write_file hello.py"
    contains "ask: 使用 shell" ask.txt "shell .*python3 hello.py"
    check "ask: hello.py 執行正確" bash -c 'python3 hello.py | grep -q "Hello agge"'

    newproject team "$MODEL"
    agge ask -y -t code "請團隊合作:先由 architect 設計並寫 PLAN.md,再由 coder 實作 fizzbuzz.py(印出 1 到 15 的 FizzBuzz,每行一個),最後你自己讀檔確認並回報" > team.txt 2>&1
    contains "team: lead 有 delegate" team.txt "↪ lead → "
    check "team: PLAN.md 存在" test -f PLAN.md
    check "team: fizzbuzz.py 第 15 行是 FizzBuzz" bash -c '[ "$(python3 fizzbuzz.py | sed -n 15p)" = FizzBuzz ]'
fi

# ------------------------------------------------------------------ all
if [[ "$MODE" == "all" ]]; then
    section "工作流程: agge code"
    newproject code "$MODEL"
    agge code -y "寫一個 Python 模組 roman.py,提供 to_roman(n) 與 from_roman(s)(1..3999,不合法輸入丟 ValueError),並用 pytest 寫測試 test_roman.py" \
        --stack "Python 3, pytest" > code.txt 2>&1
    check "code: 流程完成" grep -q "最終輸出" code.txt
    for n in design implement test review; do contains "code: 節點 $n 執行" code.txt "✔ $n"; done
    check "code: PLAN.md" test -f PLAN.md
    check "code: roman.py" test -f roman.py
    check "code: 產生的測試通過" python3 -m pytest -q
    check "code: to_roman(1994) == MCMXCIV" python3 -c "import roman; assert roman.to_roman(1994) == 'MCMXCIV'"

    section "工作流程: agge book"
    newproject book "$MODEL"
    agge book -y "給初學者的 Git 入門小書" --chapters 3 --lang 繁體中文 --style "每章約 800 字,含指令範例" > book.txt 2>&1
    check "book: 流程完成" grep -q "最終輸出" book.txt
    contains "book: 章節平行撰寫 (map)" book.txt "✔ chapters\[3\]"
    check "book: outline.md" test -s outline.md
    check "book: README.md" test -s README.md
    nch=$(find . -path ./.agge -prune -o -path ./home -prune -o -name '*.md' -print | grep -vE '^\./(README|outline)\.md$' | wc -l | tr -d ' ')
    check "book: 至少 3 個章節檔 (找到 $nch)" test "$nch" -ge 3
    short=$(find . -path ./.agge -prune -o -name '*.md' -size -1k -print | grep -vE '^\./(README|outline)\.md$' | wc -l | tr -d ' ')
    check "book: 章節不是空殼 (<1KB 的有 $short 個)" test "$short" -eq 0
fi

# ------------------------------------------------------------------ summary
section "結果"
echo "  通過 $PASS,失敗 $FAIL   (輸出保留在 $WORK)"
for f in "${FAILED[@]+"${FAILED[@]}"}"; do echo "  ✘ $f"; done
[[ $FAIL -eq 0 ]]
