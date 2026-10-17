#!/usr/bin/env bash
# two-agent-demo — 双实例协作回放样例（可回放、可录屏）。
#
# 用两个「虚拟实例」（显式身份 AB_AS）演示总线核心循环：
#   发现 → 建房 → 定向发言 → poll/ack → 任务认领/汇报 → human 终审 done → 关房。
# 这是 README Quickstart 的可执行版，也是发布冒烟判据
# （fresh venv pipx install 后本脚本须完整跑通）。
#
# 录屏（v0.1 发布物料）：asciinema rec -c ./examples/two-agent-demo.sh demo.cast
#
# 全程使用临时数据库（AB_DB），不触碰真实 ~/.agent-bus/bus.db。

set -euo pipefail

PY="${PYTHON:-python3}"
# Prefer the installed command (pipx puts it on PATH); override with
# AGENTBUS_BIN=/path/to/agentbus when running from a non-PATH install.
AB="${AGENTBUS_BIN:-agentbus}"

TMP="$(mktemp -d /tmp/agent-bus-demo.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
export AB_DB="$TMP/bus.db"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
run()  { printf '\033[2m$ %s\033[0m\n' "$*"; }

step "0) 初始化 schema（任一命令首跑自动建表），登记两个实例"
run 'agentbus whoami --as human'
$AB whoami --as human >/dev/null
$PY - "$AB_DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
# 演示用虚拟实例（生产环境由发现守护 agentbus-discover 自动登记）
for pid, kind, aid in ((9001, "codex", "codex#1"), (9002, "claude", "claude#1")):
    conn.execute(
        "INSERT OR IGNORE INTO instances(pid, kind, agent_id, state) "
        "VALUES(?,?,?,'online')", (pid, kind, aid))
conn.commit(); conn.close()
EOF
run 'agentbus ls'
$AB ls

step "1) human 建共享会话，拉两位实例入房"
run 'agentbus --as human room create --topic "登录重构" --members codex#1,claude#1'
RID="$($AB --as human room create --topic "登录重构" --members codex#1,claude#1)"
echo "room id = $RID"

step "2) codex#1 在房内发言（人类全程可见）"
run "AB_AS=codex#1 agentbus post --room $RID --body '分工：我做后端 API，claude#1 做前端联调'"
AB_AS=codex#1 $AB post --room "$RID" --body "分工：我做后端 API，claude#1 做前端联调"

step "3) claude#1 poll 取走消息并确认（消费纪律：由用户/脚本显式驱动）"
run "AB_AS=claude#1 agentbus poll"
AB_AS=claude#1 $AB poll
run "AB_AS=claude#1 agentbus ack 1"
AB_AS=claude#1 $AB ack 1

step "4) claude#1 同 room 回复，codex#1 poll 到"
run "AB_AS=claude#1 agentbus reply 1 --body '收到，我这边先出页面骨架'"
AB_AS=claude#1 $AB reply 1 --body "收到，我这边先出页面骨架"
run "AB_AS=codex#1 agentbus poll"
AB_AS=codex#1 $AB poll

step "5) 任务流：human 建任务，claude#1 原子认领"
run "agentbus --as human task create --room $RID --title '登录页重构' --priority high"
TID="$($AB --as human task create --room "$RID" --title "登录页重构" --priority high --json \
        | $PY -c 'import json,sys; print(json.load(sys.stdin)["task"]["id"])')"
echo "task id = $TID"
run "AB_AS=claude#1 agentbus task claim $TID"
AB_AS=claude#1 $AB task claim "$TID"
run "AB_AS=claude#1 agentbus busy '登录页重构'"
AB_AS=claude#1 $AB busy "登录页重构"

step "6) claude#1 汇报完成（in_review；绝不自行 done），human 终审 done"
run "AB_AS=claude#1 agentbus task report $TID --body '页面重构完成，自测通过，剩余：联调'"
AB_AS=claude#1 $AB task report "$TID" --body "页面重构完成，自测通过，剩余：联调"
run "agentbus --as human task done $TID"
$AB --as human task done "$TID"

step "7) 收尾：human 查看全量历史后关房归档"
run "agentbus --as human room show $RID --history"
$AB --as human room show "$RID" --history
run "agentbus --as human room close $RID"
$AB --as human room close "$RID"

printf '\n\033[1;32m== demo 完成：发现 → room → 定向消息 → ack → 任务认领/汇报 → human 终审 → 归档 ==\033[0m\n'
