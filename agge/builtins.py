"""Built-in agents, teams and graphs. Projects can override any of them by name."""

ALL_TOOLS = ["read_file", "write_file", "edit_file", "list_files", "search", "shell"]
FILE_TOOLS = ["read_file", "write_file", "edit_file", "list_files", "search"]

AGENTS = {
    "assistant": {
        "description": "通用助理:寫程式、寫作、操作工作區",
        "system": "You are a capable software engineer and long-form writer working directly in the user's "
                  "workspace. Inspect files before changing them, make focused changes, verify your work, "
                  "and keep answers concise.",
        "tools": ALL_TOOLS,
    },
    "lead": {
        "description": "團隊領導:拆解任務、分派給成員、整合結果",
        "system": "You lead a team of specialist agents. Break the user's request into clear subtasks, "
                  "delegate each to the most suitable member with a complete, self-contained brief "
                  "(members do not see this conversation), run independent subtasks in parallel, "
                  "check their results in the workspace, iterate when quality is lacking, "
                  "then report the outcome to the user.",
        "tools": ["read_file", "list_files", "search", "delegate"],
    },
    "architect": {
        "description": "架構師:分析需求、設計模組與介面、撰寫 PLAN.md",
        "system": "You are a senior software architect. Produce pragmatic designs: file layout, modules, "
                  "interfaces, data flow, and a test plan. Prefer simple, standard-library-first solutions.",
        "tools": FILE_TOOLS,
    },
    "coder": {
        "description": "工程師:依設計實作程式與測試",
        "system": "You are an expert programmer. Write complete, working, idiomatic code with tests. "
                  "Never leave placeholders or TODOs. Read existing files before editing them.",
        "tools": ALL_TOOLS,
    },
    "tester": {
        "description": "測試員:執行測試、回報失敗原因",
        "system": "You are a meticulous QA engineer. Run the tests and the program, read failures carefully, "
                  "and report precise, reproducible findings. Do not rewrite the implementation yourself.",
        "tools": ALL_TOOLS,
    },
    "reviewer": {
        "description": "審查者:檢查正確性與品質,決定 APPROVED / REVISE",
        "system": "You are a strict but fair code reviewer. Read the actual files. Focus on correctness, "
                  "missing requirements, bugs, and maintainability. Give concrete, actionable feedback.",
        "tools": ["read_file", "list_files", "search"],
    },
    "outliner": {
        "description": "大綱規劃:為書籍或長文設計章節結構",
        "system": "You are an experienced book editor and author who designs clear, well-paced outlines "
                  "with a logical progression and no overlap between chapters.",
        "tools": FILE_TOOLS,
    },
    "writer": {
        "description": "作者:撰寫完整章節",
        "system": "You are a skilled author. Write rich, accurate, engaging long-form prose in Markdown with "
                  "concrete examples, sections, and smooth transitions. Write the full text, never a summary.",
        "tools": FILE_TOOLS,
    },
    "editor": {
        "description": "編輯:統一風格、修正錯誤、維護目錄",
        "system": "You are a senior editor. Ensure consistency of terminology, tone and formatting across "
                  "chapters, fix factual and stylistic problems in place, and keep the table of contents current.",
        "tools": FILE_TOOLS,
    },
}

TEAMS = {
    "code": {"lead": "lead", "members": ["architect", "coder", "tester", "reviewer"],
             "description": "軟體開發團隊"},
    "book": {"lead": "lead", "members": ["outliner", "writer", "editor"],
             "description": "書籍寫作團隊"},
}

GRAPHS = {
    "solo": {
        "description": "單一 assistant 完成任務",
        "nodes": {"main": {"agent": "assistant", "prompt": "{{task}}"}},
        "edges": [],
    },
    "code": {
        "description": "設計 → 實作 → 測試 → 審查(審查不過則回到實作,最多 2 輪)",
        "nodes": {
            "design": {
                "agent": "architect",
                "prompt": "Task:\n{{task}}\n\nExplore the workspace if relevant (list_files/read_file). "
                          "Write a concise design document to PLAN.md: goals, file layout, key interfaces, "
                          "and test plan. Reply with a short summary of the design.",
            },
            "implement": {
                "agent": "coder",
                "prompt": "Task:\n{{task}}\n\nDesign summary:\n{{design}}\n\n{{feedback}}\n"
                          "Read PLAN.md, then implement everything with write_file/edit_file, including tests. "
                          "Reply with the list of files you created or changed and how to run them.",
            },
            "test": {
                "agent": "tester",
                "prompt": "Task:\n{{task}}\n\nImplementation report:\n{{implement}}\n\n"
                          "Run the tests and try the program with shell. If no tests exist, write minimal ones "
                          "and run them. Report the results and end with `RESULT: PASS` or `RESULT: FAIL`.",
            },
            "review": {
                "agent": "reviewer",
                "prompt": "Task:\n{{task}}\n\nImplementation report:\n{{implement}}\n\nTest report:\n{{test}}\n\n"
                          "Review the code by reading the files. If changes are required, end with "
                          "`VERDICT: REVISE` followed by a numbered list of required changes. "
                          "Otherwise end with `VERDICT: APPROVED`.",
            },
        },
        "edges": [
            ["design", "implement"], ["implement", "test"], ["test", "review"],
            {"from": "review", "to": "implement", "when": "VERDICT:\\s*REVISE", "max": 2},
        ],
    },
    "book": {
        "description": "大綱 → 各章平行撰寫 → 總編輯",
        "nodes": {
            "outline": {
                "agent": "outliner",
                "prompt": "Plan a book.\n\n{{task}}\n\n"
                          "1. Write the detailed outline to `outline.md`.\n"
                          "2. Write `README.md` with the book title, a preface paragraph and a table of contents "
                          "linking every chapter file.\n"
                          "3. Finish your reply with a ```json code block containing the chapter list: "
                          "[{\"file\": \"chapters/01-xxx.md\", \"title\": \"...\", \"brief\": \"what this chapter covers\"}, ...]",
            },
            "chapters": {
                "agent": "writer",
                "map": "outline",
                "prompt": "You are writing one chapter of a book.\n\nBook request:\n{{task}}\n\n"
                          "Full outline (read `outline.md` for details):\n{{outline}}\n\n"
                          "Your chapter: {{item.title}}\nFile: {{item.file}}\nBrief: {{item.brief}}\n\n"
                          "Write the COMPLETE chapter in Markdown to `{{item.file}}` with write_file "
                          "(use append=\"true\" to continue a long chapter in several parts). "
                          "Start with a `# ` heading. Reply with a 3-line summary.",
            },
            "edit": {
                "agent": "editor",
                "prompt": "Book request:\n{{task}}\n\nChapter summaries:\n{{chapters}}\n\n"
                          "Read README.md, outline.md and every chapter. Fix inconsistencies, repetition and "
                          "errors in place with edit_file, make sure README.md links every chapter correctly, "
                          "then report what you changed.",
            },
        },
        "edges": [["outline", "chapters"], ["chapters", "edit"]],
    },
    "brainstorm": {
        "description": "三位 agent 平行提案 → lead 綜合",
        "nodes": {
            "a": {"agent": "architect", "prompt": "Propose an approach for:\n{{task}}\nBe concrete."},
            "b": {"agent": "coder", "prompt": "Propose a pragmatic implementation approach for:\n{{task}}"},
            "c": {"agent": "reviewer", "prompt": "List risks, pitfalls and edge cases for:\n{{task}}"},
            "merge": {"agent": "lead", "prompt": "Task:\n{{task}}\n\n{{deps}}\n\nSynthesize the best plan."},
        },
        "edges": [["a", "merge"], ["b", "merge"], ["c", "merge"]],
    },
}
