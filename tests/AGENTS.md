# 操控自動測試

請全新乾淨部署本 skill 至 ~/.claude/skills

請用你的 computer use 能力找到在 PowerShell 上面執行的 Claude Code，注入指令開啟新的 session，載入並遵照本 skill 指示應答

新 session 開啟後，請先把 `/bookkeeping` 當成獨立的一個 turn 注入並等待載入完成；後續 prompts 不要再加 `/bookkeeping` 前綴。這個啟動 turn 也要保留在 DIALOG.md。

請讀取 prompts.txt 提示詞，以空白行分隔為多個 turns，一次輸入一個 turn，將提示詞注入 Claude Code 並等待回應完成，擷取回應結果，輸出成 DIALOG.md

## DIALOG.md 輸出格式

Dialog 由一個 turn 接著一個 turn 組成，請將輸入的提示詞開頭加上 > 表示，其後接續回應結果，有提示詞的 > 行上下皆加空白行隔開。例如：

> Dear John
>     I must let you know tonight
> that my love for you has died away
> like grass upon the lawn.

這不是記帳指令。如果您需要幫助或想要回到記帳功能，請告訴我您想做什麼

> OK 等一下，讓我想想看

好的，我在這裡等您
