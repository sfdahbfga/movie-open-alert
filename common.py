# 공통 기능: 설정 파일, 로그, 인터넷 요청, 디스코드 알림
# 원본: https://github.com/0w0i0n0g0/cgv-open-push , https://github.com/0w0i0n0g0/megabox-open-push (AGPL-3.0)
# 서버에 따로 설치할 게 없도록 파이썬 기본 기능(urllib)만 사용합니다.
import os
import gzip
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")    # 감시할 영화관 목록
SECRET_PATH = os.path.join(BASE_DIR, "secret.json")    # 디스코드 웹후크 주소 (다른 사람에게 보여주지 말 것)
LOG_PATH = os.path.join(BASE_DIR, "movie-open-alert.log")

DEFAULT_CONFIG = {
    "interval_seconds": 60,   # 1분마다 확인 (너무 짧게 줄이면 사이트에서 차단될 수 있음)
    "daily_report_hour": 9,   # 매일 이 시각(0~23시)에 '정상 작동 중' 메시지
    "sync_repo": "",          # 자동 동기화할 GitHub 저장소 (예: "아이디/movie-open-alert"). 비우면 동기화 안 함
    "targets": [],            # 예매 오픈(새 회차) 감시 목록
    "seat_watches": [],       # 빈자리 감시 목록
    "coupon_watches": [],     # 선착순 할인 쿠폰 취소분 감시 목록
    # 빈자리 감시에서 '일반석'으로 볼 값 (2026-11 실제 응답에서 확인한 값. 극장마다 다르면 여기서 고치세요)
    #   빈 리스트 [] 로 두면 그 항목은 거르지 않음
    "seat_filter": {
        "cgv": {
            "seat_kinds": ["일반석", "SWEETBOX"],  # 좌석 종류 stkndNm (빼는 것 예: 'PRIVATE 2인' = 10관 PRIVATE BOX)
            "sale_forms": ["일반"],       # 판매 형태 seatSalfrmNm (장애인석은 '이동식', 막아둔 좌석은 '판매중지')
            "zones": [],                  # 구역 szoneKindNm (예: 일반, Light존). [] = 전부 허용
        },
        "megabox": {
            "seat_classes": ["GERN_CLS", "2P_CLS"],  # 좌석 등급 seatClassCd (일반, 2인소파석. 장애인석 DISABLED_CLS는 뺌)
            "seat_zones": ["GERN_ZONE"],   # 구역 seatZoneCd
        },
        "lottecinema": {
            # 좌석 종류 = SeatBlockSet에서 처음 Y가 나오는 위치 (롯데시네마 예매 화면 코드와 같은 규칙)
            # 일반석(전부 N), 발받침석, 리클라이너석(리클라이너관은 전 좌석이 이것), 시네커플석은 포함
            # 뺀 것: 장애인석, 특별요금석, 슈퍼바이브석, 씨네비즈 등
            "seat_kinds": ["일반석", "발받침석", "리클라이너석", "시네커플석", "시네커플 발받침석"],
        },
    },
    "seat_request_gap_seconds": {"cgv": 4, "megabox": 2, "lottecinema": 3},  # 좌석 배치도 요청 사이 최소 간격(초)
    "seat_max_checks_per_cycle": 6,   # 한 번(1분)에 좌석 배치도를 최대 몇 개까지 열어볼지
    # 어떤 기기가 어떤 극장 체인을 맡을지 (여기를 바꾸면 재설치 없이 5분 안에 적용)
    "device_roles": {"server": ["megabox", "lottecinema"], "phone": ["cgv"]},
}

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


##################################################
# 파일 읽기/쓰기

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    config = load_json(CONFIG_PATH, {})
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, json.loads(json.dumps(value)))  # 기본값 복사본
    # 새로 추가된 극장 체인의 기본 설정도 채움 (예: 예전 설정 파일에 롯데시네마 좌석 필터가 없을 때)
    for key in ("seat_filter", "seat_request_gap_seconds"):
        for chain, value in DEFAULT_CONFIG[key].items():
            config[key].setdefault(chain, json.loads(json.dumps(value)))
    # 이전 버전의 기본 좌석 설정을 그대로 쓰고 있으면 새 기본값으로 갱신
    old_default = {"cgv": {"seat_kinds": ["일반석"], "sale_forms": ["일반"], "zones": []},
                   "megabox": {"seat_classes": ["GERN_CLS"], "seat_zones": ["GERN_ZONE"]}}
    if config.get("seat_filter") == old_default:
        config["seat_filter"] = json.loads(json.dumps(DEFAULT_CONFIG["seat_filter"]))
        save_config(config)
    # 예전 버전처럼 config.json에 웹후크가 들어 있으면 secret.json으로 옮김
    old_url = config.pop("discord_webhook_url", "")
    if old_url:
        if not get_webhook_url():
            set_webhook_url(old_url)
        save_config(config)
    return config


def save_config(config):
    save_json(CONFIG_PATH, config)


##################################################
# 인터넷 요청 (requests 라이브러리 대신 파이썬 기본 urllib 사용)

def http_request(url, params=None, json_body=None, form=None, headers=None, timeout=15):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept-Encoding", "gzip")  # 압축해서 받기 (데이터 사용량 약 1/10)
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body


def http_json(url, **kwargs):
    text = http_request(url, **kwargs).decode("utf-8-sig", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        # 데이터 대신 웹페이지(점검·장애·차단 안내 등)가 온 경우: 무엇이 왔는지 알림에 보이게
        import re
        title = re.search(r"<title>([^<]*)</title>", text, re.I)
        what = title.group(1).strip() if title else (text.strip()[:80] or "빈 응답")
        raise RuntimeError(f"데이터 대신 다른 응답을 받음: '{what}' (사이트 점검·장애이거나 접속이 막혔을 수 있어요)")


##################################################
# 로그 저장 (원본과 동일: 최대 5MB씩 3개 백업본)

def setup_logging():
    handlers = [RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")]
    logging.basicConfig(handlers=handlers, level=logging.INFO, format="%(asctime)s:%(levelname)s:%(message)s")


def save_log_info(log, is_log_file=False):
    if is_log_file:
        logging.info(log)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {log}", flush=True)


def save_log_error(log, is_log_file=True):
    if is_log_file:
        logging.error(log)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [오류] {log}", flush=True)


##################################################
# 디스코드 알림 (웹후크 방식)

def get_webhook_url():
    # 1순위: 환경변수, 2순위: secret.json
    return (os.environ.get("DISCORD_WEBHOOK_URL")
            or load_json(SECRET_PATH, {}).get("discord_webhook_url", ""))


def set_webhook_url(url):
    data = load_json(SECRET_PATH, {})
    data["discord_webhook_url"] = url
    save_json(SECRET_PATH, data)


def send_discord(text):
    webhook_url = get_webhook_url()
    if not webhook_url:
        save_log_info("디스코드 웹후크 주소가 없어서 화면에만 표시합니다: " + text)
        return False
    try:
        http_request(webhook_url, json_body={"content": text}, timeout=10)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"디스코드 전송 실패 ({e.code}): {e.read()[:200]!r}")
    return True


##################################################
# 날짜 표시용

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def pretty_date(yyyymmdd):
    try:
        d = datetime.strptime(yyyymmdd, "%Y%m%d")
        return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
    except ValueError:
        return yyyymmdd
