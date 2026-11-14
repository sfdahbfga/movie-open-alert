# CGV + 메가박스 예매 오픈 알리미 (실행 파일)
# 원본 로직: 처음 응답을 저장해 두고, 주기적으로 다시 조회해서 '새로 생긴 날짜'가 있으면 알림
import time
import queue
import threading
from datetime import datetime

import cgv
import megabox
from seats import seat_watch_main, Pacer
from sync import sync_main, load_device
from common import (load_config, setup_logging, save_log_info, save_log_error,
                    get_webhook_url, send_discord, pretty_date)

FETCHERS = {"cgv": cgv.fetch_showings, "megabox": megabox.fetch_showings}
CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스"}
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


def main():
    setup_logging()
    config = load_config()
    # 이 기기가 맡은 체인만 실행 (오라클 서버 = 메가박스, 휴대폰 = CGV). PC처럼 device.json이 없으면 전부.
    device_chains = set(load_device().get("chains") or [])
    targets = [t for t in config["targets"] if not device_chains or t["chain"] in device_chains]
    seat_watches = [w for w in config.get("seat_watches") or [] if not device_chains or w["chain"] in device_chains]
    interval = max(30, int(config.get("interval_seconds", 60)))

    threading.Thread(target=sender_main, daemon=True).start()
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
    pacers = {chain: Pacer(float(gaps.get(chain, 4))) for chain in ("cgv", "megabox")}
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
