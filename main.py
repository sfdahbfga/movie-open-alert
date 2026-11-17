# CGV + 메가박스 예매 오픈 알리미 (실행 파일)
# 원본 로직: 처음 응답을 저장해 두고, 주기적으로 다시 조회해서 '새로 생긴 날짜'가 있으면 알림
import os
import time
import queue
import threading
from datetime import datetime

import cgv
import megabox
from seats import seat_watch_main, Pacer
from sync import sync_main, device_chains
try:
    import lotte   # 롯데시네마 (파일이 아직 없는 옛 설치본에서도 멈추지 않게)
except ImportError:
    lotte = None
from common import (BASE_DIR, load_config, setup_logging, save_log_info, save_log_error,
                    get_webhook_url, send_discord, pretty_date)

FETCHERS = {"cgv": cgv.fetch_showings, "megabox": megabox.fetch_showings}
if lotte:
    FETCHERS["lottecinema"] = lotte.fetch_showings
CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스", "lottecinema": "롯데시네마"}
ERROR_ALERT_COUNT = 10  # 이만큼 연속으로 실패하면 디스코드로 경고

message_queue = queue.Queue()
status = {}  # 영화관별 마지막 상태 (매일 보고용)


def format_showings(items, with_movie):
    """'날짜|시간|상영관|영화|형식' 목록을 날짜별로 묶어 보기 좋게"""
    by_date = {}
    for item in sorted(items):
        date, time_, screen, movie, kind = (item.split("|") + [""] * 5)[:5]
        text = f"{time_} {screen}".strip()
        if kind:
            text += f" {kind}"
        if with_movie and movie:
            text += f" «{movie}»"
        by_date.setdefault(date, []).append(text)
    return "\n".join(f"· {pretty_date(d)}: " + ", ".join(v) for d, v in by_date.items())


def target_main(target, interval):
    name = target["name"]
    fetch = FETCHERS[target["chain"]]
    with_movie = not (target.get("movie_keyword") or target.get("movie_no"))
    seen = None   # 지금까지 한 번이라도 본 회차 (켤 때 이미 열려 있던 회차는 알리지 않음)
    error_count = 0
    while True:
        try:
            current = fetch(target)
            if error_count >= ERROR_ALERT_COUNT:
                message_queue.put(f"✅ [{name}] 조회가 다시 정상화됐습니다.")
            error_count = 0
            status[name] = f"정상 (회차 {len(current)}개)"
            today = datetime.now().strftime("%Y%m%d")

            if seen is None:
                seen = set(current)
                dates = sorted({x.split("|")[0] for x in current})
                save_log_info(f"{name} 감시 시작. 회차 {len(current)}개, 날짜: "
                              f"{', '.join(pretty_date(d) for d in dates) or '없음'}", True)
            else:
                # 처음 보는 회차만 알림 (일시적으로 사라졌다 다시 나타난 회차는 중복 알림 안 함)
                added = {x for x in current - seen if x.split("|")[0] >= today}
                if added:
                    save_log_info(f"{name} added item : {sorted(added)}", True)
                    message_queue.put(f"🎬 **예매 오픈 알림** [{name}]\n{format_showings(added, with_movie)}")
                seen |= current
                # 지난 날짜 기록은 정리
                seen = {x for x in seen if x.split("|")[0] >= today}
                save_log_info(f"{name} 확인 완료 (회차 {len(current)}개, 새 회차 {len(added)}개)")
            time.sleep(interval)
        except Exception as e:
            error_count += 1
            status[name] = f"조회 실패 {error_count}회 연속 ({e})"
            save_log_error(f"{name} error ({error_count}회 연속) : {e}")
            if error_count == ERROR_ALERT_COUNT:
                message_queue.put(f"⚠️ [{name}] 조회가 {ERROR_ALERT_COUNT}번 연속 실패했습니다.\n"
                                  f"원인: {e}\n(사이트 구조가 바뀌었거나 접속이 막혔을 수 있어요)")
            time.sleep(60)  # 1분 대기 후 다시 시도


def sender_main():
    while True:
        text = message_queue.get()
        print("\n" + "=" * 50 + f"\n{text}\n" + "=" * 50, flush=True)
        for _ in range(3):
            try:
                send_discord(text)
                break
            except Exception as e:
                save_log_error(f"알림 전송 오류 : {e}")
                time.sleep(5)  # 5초 대기 후 다시 알림 보내기 시도


def daily_report_main(report_hour):
    # 매일 정해진 시각에 '정상 작동 중' 메시지 → 이 메시지가 안 오면 서버가 멈춘 것
    last_sent = None
    while True:
        now = datetime.now()
        if now.hour == report_hour and last_sent != now.date():
            last_sent = now.date()
            lines = [f"🟢 예매 오픈 알리미 정상 작동 중 ({now.strftime('%m/%d %H:%M')})"]
            lines += [f"· {name}: {state}" for name, state in status.items()]
            message_queue.put("\n".join(lines))
        time.sleep(30)



##################################################
# 멈춤 감지 + 자동 재시작 (휴대폰용)
# - 프로그램이 1분마다 heartbeat.txt에 현재 시각을 적음
# - 휴대폰에서는 watchdog.sh를 만들어 둠. 안드로이드 예약 작업(termux-job-scheduler)이
#   15분마다 이 스크립트를 실행하고, 기록이 10분 넘게 멈춰 있으면 알리미를 다시 켬
# - 다시 켜지면 '언제부터 얼마나 멈췄는지'를 디스코드로 알림

HEARTBEAT_PATH = os.path.join(BASE_DIR, "heartbeat.txt")
WATCHDOG_MARK_PATH = os.path.join(BASE_DIR, "watchdog_restart.txt")
WATCHDOG_PATH = os.path.join(BASE_DIR, "watchdog.sh")
STALL_SECONDS = 600
WATCHDOG_SCRIPT = """#!/data/data/com.termux/files/usr/bin/bash
# 알리미가 10분 넘게 멈춰 있으면 다시 켬 (안드로이드 예약 작업이 15분마다 실행)
cd "$HOME/movie-open-alert" || exit 0
now=$(date +%s)
last=$(cat heartbeat.txt 2>/dev/null || echo 0)
if [ $((now - last)) -gt 600 ]; then
  echo "$now" > watchdog_restart.txt
  pkill -f movie-open-alert/run.sh
  pkill -f "python -u main.py"
  sleep 2
  nohup "$HOME/movie-open-alert/run.sh" >/dev/null 2>&1 &
fi
"""


def _read_int(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_heartbeat():
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def heartbeat_main():
    while True:
        write_heartbeat()
        time.sleep(60)


def previous_stop_message():
    """지난번에 오래 멈춰 있었다면 알림 문구를 돌려줌"""
    last = _read_int(HEARTBEAT_PATH)
    by_watchdog = os.path.exists(WATCHDOG_MARK_PATH)
    if by_watchdog:
        try:
            os.remove(WATCHDOG_MARK_PATH)
        except OSError:
            pass
    gap = time.time() - last
    if not last or gap <= STALL_SECONDS:
        return None
    start = datetime.fromtimestamp(last).strftime("%m/%d %H:%M")
    minutes = int(gap // 60)
    length = f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60 else f"{minutes}분"
    how = "자동 확인 장치가 다시 켰어요." if by_watchdog else "다시 켜졌어요."
    return (f"⚠️ 이 기기의 알리미가 {start}부터 약 {length} 동안 멈춰 있다가 {how}\n"
            f"· 그동안 이 기기가 맡은 감시는 쉬었어요.")


def install_watchdog():
    """휴대폰(Termux)에서만 watchdog.sh를 만들어 둠 (내용이 같으면 그대로)"""
    if "com.termux" not in os.environ.get("PREFIX", ""):
        return
    try:
        old = open(WATCHDOG_PATH, encoding="utf-8").read() if os.path.exists(WATCHDOG_PATH) else ""
        if old != WATCHDOG_SCRIPT:
            with open(WATCHDOG_PATH, "w", encoding="utf-8", newline="\n") as f:
                f.write(WATCHDOG_SCRIPT)
        os.chmod(WATCHDOG_PATH, 0o755)
    except OSError as e:
        save_log_error(f"[감시 장치] watchdog.sh를 만들지 못함: {e}")


def main():
    setup_logging()
    config = load_config()
    # 이 기기가 맡은 체인만 실행 (오라클 서버 = 메가박스, 휴대폰 = CGV). PC처럼 device.json이 없으면 전부.
    chains = device_chains(config)
    # 이 기기가 맡은 체인 + 지금 실행할 수 있는 체인만
    targets = [t for t in config["targets"]
               if (not chains or t["chain"] in chains) and t["chain"] in FETCHERS]
    seat_watches = [w for w in config.get("seat_watches") or []
                    if (not chains or w["chain"] in chains) and w["chain"] in FETCHERS]
    interval = max(30, int(config.get("interval_seconds", 60)))

    threading.Thread(target=sender_main, daemon=True).start()
    stop_msg = previous_stop_message()
    if stop_msg:
        save_log_info(stop_msg.replace("\n", " "), True)
        message_queue.put(stop_msg)
    threading.Thread(target=heartbeat_main, daemon=True).start()
    install_watchdog()
    threading.Thread(target=sync_main, args=(message_queue,), daemon=True).start()
    if not targets and not seat_watches:
        print("이 기기에서 감시할 대상이 없습니다. (저장소에 추가되면 자동으로 시작돼요)")
        message_queue.put("ℹ️ 알리미가 켜졌지만 이 기기에서 감시할 대상이 없어요. 저장소에 추가되면 자동으로 시작돼요.")
        while True:
            time.sleep(60)

    print(f"예매 오픈 알리미 시작 — 예매 오픈 {len(targets)}곳, 빈자리 {len(seat_watches)}건 감시, "
          f"{interval}초마다 확인 (종료: Ctrl+C)")
    for t in targets:
        print(f"  - [{CHAIN_NAME[t['chain']]}] {t['name']}")
    for w in seat_watches:
        print(f"  - [{CHAIN_NAME[w['chain']]} 빈자리] {w['name']}")
    if not get_webhook_url():
        print("※ 디스코드 웹후크가 설정되지 않아 이 창에만 알림이 표시됩니다.")

    threading.Thread(target=daily_report_main, args=(int(config.get("daily_report_hour", 9)),), daemon=True).start()
    lines = [f"✅ 예매 오픈 알리미 시작 ({interval}초 간격)"]
    lines += [f"· 예매 오픈: {t['name']}" for t in targets]
    lines += [f"· 빈자리: {w['name']}" for w in seat_watches]
    message_queue.put("\n".join(lines))

    for t in targets:
        threading.Thread(target=target_main, args=(t, interval), daemon=True).start()
        time.sleep(1)  # 원본처럼 1초 간격으로 시작

    # 좌석 배치도 요청은 사이트별로 하나의 '간격 조절기'를 같이 씀 (여러 감시가 동시에 몰리지 않게)
    gaps = config.get("seat_request_gap_seconds") or {}
    pacers = {chain: Pacer(float(gaps.get(chain, 4))) for chain in FETCHERS}
    for w in seat_watches:
        threading.Thread(target=seat_watch_main,
                         args=(w, config, message_queue, pacers[w["chain"]], status, interval, ERROR_ALERT_COUNT),
                         daemon=True).start()
        time.sleep(3)  # 감시를 3초 간격으로 시작해서 요청이 한꺼번에 몰리지 않게

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n알리미를 종료합니다.")


if __name__ == "__main__":
    main()
