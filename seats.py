# 빈자리 감시
# 1단계) 1분마다 회차 목록의 '잔여석 숫자'만 확인 (가벼운 요청)
# 2단계) 숫자가 바뀐 회차만 좌석 배치도를 열어 '일반석'인 빈 좌석만 셈
#        (처음 감시를 시작할 때도 한 번씩 2단계 확인)
# 같은 좌석은 한 번만 알리고, 그 좌석이 팔린 걸 확인한 뒤 다시 비면 그때 다시 알림.
# 자동 예매/결제는 하지 않음. 로그인도 하지 않음.
import time
import threading
from datetime import datetime

import os
import json

import cgv
import megabox
from common import BASE_DIR, load_json, save_json, save_log_info, save_log_error, pretty_date

try:
    import lotte
except ImportError:
    lotte = None
SITES = {"cgv": cgv, "megabox": megabox}
if lotte:
    SITES["lottecinema"] = lotte
CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스", "lottecinema": "롯데시네마"}
MAX_SEATS_IN_MESSAGE = 30
STATE_PATH = os.path.join(BASE_DIR, "seat_state.json")  # 다시 시작해도 '이미 알린 좌석'을 기억하기 위한 파일
_state_lock = threading.Lock()


def _load_state(watch_name):
    with _state_lock:
        return load_json(STATE_PATH, {}).get(watch_name, {})


def _save_state(watch_name, data):
    with _state_lock:
        all_state = load_json(STATE_PATH, {})
        all_state[watch_name] = data
        tmp = STATE_PATH + ".tmp"
        save_json(tmp, all_state)
        os.replace(tmp, STATE_PATH)


class Pacer:
    """같은 사이트로 보내는 좌석 배치도 요청 사이에 최소 간격을 둠 (차단 방지)"""

    def __init__(self, gap_seconds):
        self.gap = gap_seconds
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


def _seat_sort_key(name):
    row = "".join(c for c in name if not c.isdigit())
    num = "".join(c for c in name if c.isdigit())
    return (row, int(num) if num else 0)


class SeatWatcher:
    def __init__(self, watch, config, message_queue, pacer):
        self.watch = watch
        self.name = watch["name"]
        self.site = SITES[watch["chain"]]
        self.rule = (config.get("seat_filter") or {}).get(watch["chain"], {})
        self.max_checks = int(config.get("seat_max_checks_per_cycle", 6))
        self.queue = message_queue
        self.pacer = pacer
        self.last_rest = {}      # 회차별 마지막으로 본 잔여석 숫자
        self.notified = {}       # 회차별 '이미 알린 빈 일반석' 집합
        self.pending = []        # 2단계 확인이 필요한 회차 키 (요청 수 제한 때문에 다음 차례로 밀린 것 포함)
        self.dates = {}          # 회차별 날짜 (기록 정리용)
        self.unknown_logged = set()
        # 감시 조건이 같으면 이전 기록을 이어서 씀 (조건이 바뀌면 새로 시작)
        self.state_id = self.name + "|" + json.dumps(watch, ensure_ascii=False, sort_keys=True)
        saved = _load_state(self.state_id)
        today = datetime.now().strftime("%Y%m%d")
        for key, v in saved.items():
            if v.get("date", "") >= today:
                self.last_rest[key] = v.get("rest")
                self.notified[key] = set(v.get("notified") or [])
                self.dates[key] = v.get("date", "")
        self._saved_snapshot = None

    def save_state(self):
        data = {k: {"rest": self.last_rest.get(k), "notified": sorted(self.notified.get(k, set())),
                    "date": self.dates.get(k, "")} for k in self.last_rest}
        snapshot = json.dumps(data, sort_keys=True)
        if snapshot != self._saved_snapshot:
            _save_state(self.state_id, data)
            self._saved_snapshot = snapshot

    # ---------- 1단계 ----------
    def check_counts(self):
        showings = self.site.list_seat_showings(self.watch)
        today = datetime.now().strftime("%Y%m%d")
        by_key = {}
        for s in showings:
            if s["date"] < today:
                continue
            by_key[s["key"]] = s
            self.dates[s["key"]] = s["date"]
            prev = self.last_rest.get(s["key"])
            self.last_rest[s["key"]] = s["rest"]
            if prev is None:
                # 처음 본 회차: 2단계 확인 한 번 (이미 비어 있는 일반석이 있으면 알림)
                reason = "first"
            elif s["rest"] != prev:
                # 늘었으면 빈자리 확인, 줄었으면 어떤 좌석이 팔렸는지 기록 갱신용
                reason = "changed"
            else:
                continue
            if s["key"] not in self.pending:
                self.pending.append(s["key"])
            save_log_info(f"[빈자리] {self.name} {pretty_date(s['date'])} {s['time']} 잔여 {prev}→{s['rest']} ({reason})")
        # 사라진 회차 정리
        for key in list(self.last_rest):
            if key not in by_key:
                self.last_rest.pop(key, None)
                self.notified.pop(key, None)
        self.pending = [k for k in self.pending if k in by_key]
        return by_key

    # ---------- 2단계 ----------
    def check_seats(self, showing):
        self.pacer.wait()
        seats = self.site.get_seats(showing["seat_args"])
        free_normal = set()
        for seat in seats:
            if not seat["free"]:
                continue
            if self.site.is_normal_seat(seat, self.rule):
                free_normal.add(seat["name"])
            else:
                desc = self.site.describe_seat(seat)
                if desc not in self.unknown_logged:
                    self.unknown_logged.add(desc)
                    save_log_info(f"[빈자리] {self.name}: 일반석이 아니라서 제외한 빈 좌석 종류 = {desc} "
                                  f"(일반석으로 보려면 config.json의 seat_filter에 추가)", True)
        key = showing["key"]
        already = self.notified.get(key, set())
        # 지금 비어 있지 않은 좌석은 '이미 알림' 기록에서 지움 → 나중에 다시 비면 다시 알림
        already &= free_normal
        new_seats = free_normal - already
        self.notified[key] = already | free_normal
        save_log_info(f"[빈자리] {self.name} {pretty_date(showing['date'])} {showing['time']}: "
                      f"빈 일반석 {len(free_normal)}개, 새로 알릴 좌석 {len(new_seats)}개", True)
        if new_seats:
            self.queue.put(self.format_message(showing, new_seats, len(free_normal)))

    def format_message(self, s, new_seats, total_free):
        names = sorted(new_seats, key=_seat_sort_key)
        shown = ", ".join(names[:MAX_SEATS_IN_MESSAGE])
        if len(names) > MAX_SEATS_IN_MESSAGE:
            shown += f" 외 {len(names) - MAX_SEATS_IN_MESSAGE}석"
        return (f"💺 **빈자리 알림** [{self.name}]\n"
                f"극장: {s['theater']}\n"
                f"영화: {s['movie']}\n"
                f"일시: {pretty_date(s['date'])} {s['time']}\n"
                f"상영관: {s['screen']}\n"
                f"빈 일반석 (새로 빈 {len(names)}석 / 현재 총 {total_free}석): {shown}\n"
                f"예매: {self.site.BOOKING_URL}")

    def run_once(self):
        by_key = self.check_counts()
        checked = 0
        while self.pending and checked < self.max_checks:
            key = self.pending.pop(0)
            if key in by_key:
                self.check_seats(by_key[key])
                checked += 1
        if self.pending:
            save_log_info(f"[빈자리] {self.name}: 좌석 확인 {len(self.pending)}개는 다음 차례로 미룸 (요청 수 제한)")
        self.save_state()


def seat_watch_main(watch, config, message_queue, pacer, status, interval, error_alert_count=10):
    watcher = SeatWatcher(watch, config, message_queue, pacer)
    name = watch["name"]
    error_count = 0
    first = True
    while True:
        try:
            watcher.run_once()
            if error_count >= error_alert_count:
                message_queue.put(f"✅ [{name}] 빈자리 조회가 다시 정상화됐습니다.")
            error_count = 0
            status[name] = f"빈자리 감시 정상 (회차 {len(watcher.last_rest)}개)"
            if first:
                first = False
                save_log_info(f"[빈자리] {name} 감시 시작. 조건에 맞는 회차 {len(watcher.last_rest)}개", True)
            time.sleep(interval)
        except Exception as e:
            error_count += 1
            status[name] = f"빈자리 조회 실패 {error_count}회 연속 ({e})"
            save_log_error(f"[빈자리] {name} error ({error_count}회 연속) : {e}")
            if error_count == error_alert_count:
                message_queue.put(f"⚠️ [{name}] 빈자리 조회가 {error_alert_count}번 연속 실패했습니다.\n"
                                  f"원인: {e}\n(사이트 구조가 바뀌었거나 접속이 막혔을 수 있어요)")
            # 실패가 이어지면 점점 더 천천히 재시도 (최대 10분) → 차단 위험 줄이기
            time.sleep(min(60 * error_count, 600))
