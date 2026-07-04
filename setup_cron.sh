#!/bin/bash
set -e

APPDIR=/home/ubuntu/naver_rank_vps_upload
FLAG="$APPDIR/.auto_refresh_enabled"

# 기본값: 자동 갱신 켜짐. 끄고 싶으면: rm /home/ubuntu/naver_rank_vps_upload/.auto_refresh_enabled
# 다시 켜고 싶으면: touch /home/ubuntu/naver_rank_vps_upload/.auto_refresh_enabled
touch "$FLAG"

cat > "$APPDIR/daily_refresh.sh" <<'INNEREOF'
#!/bin/bash
APPDIR=/home/ubuntu/naver_rank_vps_upload
FLAG="$APPDIR/.auto_refresh_enabled"
LOG="$APPDIR/cron_refresh.log"

if [ ! -f "$FLAG" ]; then
    echo "$(date): 자동 갱신 꺼져있음 (건너뜀)" >> "$LOG"
    exit 0
fi

echo "$(date): 자동 갱신 시작" >> "$LOG"
curl -s -X POST http://127.0.0.1:8080/api/refresh_all -H "Content-Type: application/json" -d '{"user_id":"대영"}' >> "$LOG" 2>&1
echo "" >> "$LOG"
curl -s -X POST http://127.0.0.1:8080/api/refresh_all -H "Content-Type: application/json" -d '{"user_id":"서희"}' >> "$LOG" 2>&1
echo "" >> "$LOG"
echo "$(date): 자동 갱신 완료" >> "$LOG"
INNEREOF

chmod +x "$APPDIR/daily_refresh.sh"

CRON_LINE="30 12 * * * $APPDIR/daily_refresh.sh"
( crontab -l 2>/dev/null | grep -v "daily_refresh.sh" ; echo "$CRON_LINE" ) | crontab -

echo "설치된 crontab:"
crontab -l
echo "---"
echo "자동 갱신 스크립트: $APPDIR/daily_refresh.sh"
echo "끄려면: rm $FLAG"
echo "켜려면: touch $FLAG"
