#!/bin/bash
# dispatch.sh — ส่งคำสั่งจาก pane หนึ่งไปอีก pane (generic, อ่านค่าจาก team.conf)
#
# Usage:
#   dispatch.sh [--no-goal] <role> "<message>"
#
# role: ค่าใดค่าหนึ่งใน TITLES ของ team.conf
# message: ข้อความที่จะส่งไปเป็น user input ของ claude pane นั้น
#
# พฤติกรรมเริ่มต้น (goal mode):
#   เมื่อ dispatch ไปยัง agent อื่น สคริปต์จะ "ห่อ" ข้อความด้วย /goal ให้อัตโนมัติ
#   → agent ปลายทางทำงานต่อเนื่องจนเสร็จ ไม่หยุดถาม confirm ทีละ step
#   (ยกเว้นข้อ safety ใน commands/goal.md)
#
#   จึงควรเขียน <message> ให้เป็น "เงื่อนไขความสำเร็จ" ที่ตรวจสอบได้ เช่น
#   "อ่าน tasks/qa-login.md ทำให้เสร็จ และเขียน done/qa-login.md ให้ครบ"
#
# ตัวเลือก:
#   --no-goal   ส่งข้อความธรรมดา ไม่ห่อด้วย /goal (เช่น ping/แจ้งสถานะสั้น ๆ)
#   planner     ไม่เคยห่อด้วย /goal (กันตั้ง goal ทับ planner เอง)
#
# env:
#   DRY_RUN=1   พิมพ์คำสั่งที่จะส่งโดยไม่ส่งจริง
#   TEAM_CONF   ระบุ path ของ team.conf เอง

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# หา team.conf: ที่เดียวกับสคริปต์ก่อน แล้วค่อยไล่ไปที่ที่ kit ถูกติดตั้ง
CONF="${TEAM_CONF:-}"
if [[ -z "$CONF" ]]; then
  # ../../ = root ของ repo ซึ่งเป็นที่ที่ team.conf.example บอกให้วางไฟล์จริง
  # (ขาดบรรทัดนี้ไป dispatch.sh เลยหา team.conf ที่ root ไม่เจอ ต้องตั้ง TEAM_CONF เอง)
  for c in "$SCRIPT_DIR/team.conf" \
           "$SCRIPT_DIR/../team.conf" \
           "$SCRIPT_DIR/../../team.conf" \
           "$SCRIPT_DIR/../../references/agents-team/team.conf"; do
    [[ -f "$c" ]] && { CONF="$c"; break; }
  done
fi
if [[ -z "$CONF" || ! -f "$CONF" ]]; then
  echo "ไม่พบ team.conf (ตั้ง TEAM_CONF=/path/to/team.conf ได้)" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONF"

: "${SESSION:?team.conf ต้องกำหนด SESSION}"
: "${WINDOW:?team.conf ต้องกำหนด WINDOW}"

USE_GOAL=1

while [[ $# -gt 0 && "${1:-}" == --* ]]; do
  case "$1" in
    --no-goal) USE_GOAL=0; shift ;;
    --goal)    USE_GOAL=1; shift ;;
    --) shift; break ;;
    *) echo "Unknown option: $1 (ใช้ --no-goal | --goal)" >&2; exit 1 ;;
  esac
done

VALID="$(printf '%s|' "${TITLES[@]}")"; VALID="${VALID%|}"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 [--no-goal] <$VALID> \"<message>\"" >&2
  exit 1
fi

ROLE="$1"; shift
MSG="$*"

known=0
for t in "${TITLES[@]}"; do [[ "$t" == "$ROLE" ]] && { known=1; break; }; done
if [[ "$known" -eq 0 ]]; then
  echo "Unknown role: $ROLE (ใช้ $VALID)" >&2
  exit 1
fi
[[ "$ROLE" == "planner" ]] && USE_GOAL=0   # ไม่ห่อ /goal ให้ตัว planner เอง

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' ไม่มี — รัน orchestrator.sh ก่อน" >&2
  exit 1
fi

# หา pane จาก tmux user option "@role" ของ pane นั้น ไม่ใช่จาก pane index
#
# ทำไมไม่ใช้ index: tmux เรียง pane index ใหม่ตามตำแหน่งหลัง split-window + select-layout
# เจอจริงว่าเพี้ยนไป 1 ตำแหน่ง → dispatch ไปผิด role โดยไม่มี error ใด ๆ
#
# ทำไมไม่ใช้ pane_title: Claude Code เขียนทับ pane title เองตามงานที่กำลังทำ
#
# @role เป็น user option ของ tmux เอง ไม่มีใครไปยุ่ง → ตั้งครั้งเดียวใน orchestrator.sh
# หาไม่เจอ = ล้มเหลวเสียงดัง ดีกว่าส่งไปผิด pane เงียบ ๆ
PANE="$(tmux list-panes -t "$SESSION:$WINDOW" -F '#{pane_index} #{@role}' \
        | awk -v r="$ROLE" '$2 == r { print $1; exit }')"

if [[ -z "$PANE" ]]; then
  echo "หา pane ของ role '$ROLE' ไม่เจอ (@role ยังไม่ถูกตั้ง?)" >&2
  echo "ตั้งให้ครบก่อน เช่น: tmux set-option -p -t $SESSION:$WINDOW.<index> @role $ROLE" >&2
  echo "ตอนนี้มี:" >&2
  tmux list-panes -t "$SESSION:$WINDOW" -F '  index=#{pane_index} @role=#{@role} title=#{pane_title}' >&2
  exit 1
fi

if [[ "$USE_GOAL" -eq 1 ]]; then
  SEND="/goal $MSG"
else
  SEND="$MSG"
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[DRY_RUN] target=$SESSION:$WINDOW.$PANE ($ROLE) goal_mode=$USE_GOAL"
  echo "[DRY_RUN] would send: $SEND"
  exit 0
fi

TARGET="$SESSION:$WINDOW.$PANE"

# พิมพ์ข้อความเป็น literal (ปลอดภัยกับอักขระพิเศษ)
# กรณี /goal: ช่องว่างหลัง "/goal" ปิด slash-command palette ทำให้ args ตามหลังถูกส่งครบ
tmux send-keys -t "$TARGET" -l -- "$SEND"
# หน่วงก่อน Enter — กับข้อความยาว/bracketed-paste ถ้ายิง Enter เร็วเกิน
# มันจะหลุดเข้าไปใน paste buffer แล้วไม่ submit (อาการ "ลืมกด enter")
sleep 0.6
tmux send-keys -t "$TARGET" C-m
# กันพลาด: ถ้า Enter แรกไปตกใน paste ที่ยังไม่ปิด ส่งซ้ำอีกครั้งหลังหน่วงสั้น ๆ
sleep 0.3
tmux send-keys -t "$TARGET" C-m

if [[ "$USE_GOAL" -eq 1 ]]; then
  echo "→ ส่งให้ $ROLE (pane $PANE) แบบ /goal แล้ว: $MSG"
else
  echo "→ ส่งให้ $ROLE (pane $PANE) แล้ว: $MSG"
fi
