# 롯데시네마 조회 기능 (로그인 없이 동작하는 것을 2026-11에 확인한 요청만 사용)
# 모든 요청은 TicketingData.aspx에 'paramList'(JSON 문자열)를 담아 POST로 보냄
import json
from datetime import datetime
from common import USER_AGENT, http_json

URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
BOOKING_URL = "https://www.lottecinema.co.kr/NLCHS/Ticketing"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.lottecinema.co.kr",
    "Referer": BOOKING_URL,
    "User-Agent": USER_AGENT,
}

# 좌석 종류: SeatBlockSet(Y/N 목록)에서 처음 Y가 나오는 위치 → 이름 (롯데시네마 예매 화면 코드와 같은 규칙)
SEAT_KIND_BY_POSITION = {
    0: "시네커플석", 1: "시네패밀리석", 2: "슈퍼바이브석", 3: "장애인석", 4: "일반석", 5: "일반석",
    6: "발받침석", 7: "시네커플 발받침석", 8: "장애인 발받침석", 9: "일반석", 10: "특별요금석",
    11: "시네패밀리 발받침석", 12: "슈퍼바이브 발받침석", 13: "씨네비즈", 14: "리클라이너석",
}
FREE_STATUS = 0  # 빈 좌석 상태 코드 (팔린 좌석은 50, 30 등으로 옴)


def _call(method, **params):
    param_list = {"MethodName": method, "channelType": "HO", "osType": "W", "osVersion": USER_AGENT, **params}
    data = http_json(URL, form={"paramList": json.dumps(param_list, ensure_ascii=False)}, headers=HEADERS)
    if str(data.get("IsOK")).lower() != "true":
        raise RuntimeError(f"롯데시네마 응답 오류: {data.get('ResultMessage')}")
    return data


def _items(obj):
    return (obj or {}).get("Items") or []


def cinema_code(t):
    """'1|0003|9101' 형식 (지역구분|세부지역|극장번호)"""
    return f"{t['division']}|{t['detail']}|{t['cinema_id']}"


def get_all_theaters():
    data = _call("GetTicketingPageTOBE", memberOnNo="0")
    result = {}
    for c in _items((data.get("Cinemas") or {}).get("Cinemas")):
        if int(c.get("DivisionCode") or 0) != 1:
            continue  # 2는 특별관 묶음(같은 극장이 중복으로 나옴)
        result[c["CinemaID"]] = {"division": 1, "detail": c["DetailDivisionCode"],
                                 "cinema_id": c["CinemaID"], "name": c["CinemaNameKR"]}
    return sorted(result.values(), key=lambda t: t["name"])


def search_theaters(keyword):
    keyword = keyword.replace(" ", "")
    return [t for t in get_all_theaters() if keyword in t["name"].replace(" ", "")]


def get_open_dates(target):
    """그 극장에서 예매가 열린 날짜 -> ['20261117', ...]"""
    data = _call("GetInvisibleMoviePlayInfo", cinemaList=cinema_code(target), movieCd="",
                 playDt=datetime.now().strftime("%Y-%m-%d"))
    dates = set()
    for d in _items(data.get("PlayDates")):
        s = str(d.get("PlayDate") or "")[:10]   # '2026-11-17 오전 12:00:00' 형태
        if len(s) == 10:
            dates.add(s.replace("-", ""))
    return sorted(dates)


def get_schedule(target, ymd):
    data = _call("GetPlaySequence", playDate=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                 cinemaID=cinema_code(target), representationMovieCode="")
    return _items(data.get("PlaySeqs"))


def _screen_name(row):
    return str(row.get("BrandNm_KR") or row.get("ScreenNameKR") or "").strip()


SHOWING_TEXT_FIELDS = ("MovieNameKR", "ScreenNameKR", "BrandNm_KR", "ScreenDivisionNameKR", "FilmNameKR",
                       "TranslationDivisionNameKR", "AccompanyTypeNameKR", "SequenceNoGroupNameKR",
                       "BkShowLabelTxtNm1", "BkShowLabelTxtNm2", "FourDTypeNameKR", "SoundTypeNameKR")


def _match_text(row, keyword, fields):
    if not keyword:
        return True
    text = " ".join(str(row.get(k) or "") for k in fields).replace(" ", "")
    return keyword.replace(" ", "") in text


##################################################
# 예매 오픈 감시

def fetch_showings(target):
    """감시 대상의 '예매 가능한 회차' 집합 ('날짜|시작시간|상영관|영화|형식')"""
    result = set()
    for ymd in get_open_dates(target):
        for row in get_schedule(target, ymd):
            if row.get("IsBookingYN") == "N":
                continue
            if not _match_text(row, target.get("movie_keyword", ""), ("MovieNameKR",)):
                continue
            if not _match_text(row, target.get("screen_keyword", ""),
                               ("ScreenNameKR", "BrandNm_KR", "ScreenDivisionNameKR", "FilmNameKR")):
                continue
            result.add("|".join([ymd, str(row.get("StartTime") or ""), _screen_name(row),
                                 str(row.get("MovieNameKR") or ""), str(row.get("FilmNameKR") or "")]))
    return result


##################################################
# 빈자리 감시

def list_seat_showings(watch):
    """1단계: 조건에 맞는 회차와 '잔여석 숫자' (회차 목록 요청만 사용)"""
    wanted = set(watch.get("dates") or [])
    times = set(watch.get("times") or [])
    result = []
    for ymd in get_open_dates(watch):
        if wanted and ymd not in wanted:
            continue
        for row in get_schedule(watch, ymd):
            if row.get("IsBookingYN") == "N":
                continue
            if not _match_text(row, watch.get("movie_keyword", ""), ("MovieNameKR",)):
                continue
            if not _match_text(row, watch.get("showing_keyword", ""), SHOWING_TEXT_FIELDS):
                continue
            time_ = str(row.get("StartTime") or "")
            if times and time_ not in times:
                continue
            result.append({
                "key": f"lottecinema|{row.get('CinemaID')}|{row.get('ScreenID')}|{ymd}|{row.get('PlaySequence')}",
                "date": ymd, "time": time_,
                "theater": "롯데시네마 " + str(row.get("CinemaNameKR") or watch.get("name", "")),
                "screen": _screen_name(row),
                "movie": str(row.get("MovieNameKR") or ""),
                "rest": int(row.get("BookingSeatCount") or 0),
                "seat_args": {"cinemaId": row.get("CinemaID"), "screenId": row.get("ScreenID"),
                              "playDate": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                              "playSequence": row.get("PlaySequence"),
                              "screenDivisionCode": row.get("ScreenDivisionCode")},
            })
    return result


def seat_kind(block_set):
    pos = str(block_set or "").replace(",", "").find("Y")
    if pos < 0:
        return "일반석"
    return SEAT_KIND_BY_POSITION.get(pos, f"알수없음({pos})")


def get_seats(seat_args):
    """2단계: 좌석 배치도 -> [{name, kind, free}]"""
    data = _call("GetSeats", **seat_args)
    sold = {b.get("SeatNo") for b in _items(data.get("BookingSeats"))}
    seats = []
    for s in _items(data.get("Seats")):
        seats.append({
            "name": f"{s.get('ShowSeatRow') or s.get('SeatRow') or ''}{s.get('ShowSeatColumn') or s.get('SeatColumn') or ''}",
            "kind": seat_kind(s.get("SeatBlockSet")),
            "free": s.get("SeatStatusCode") == FREE_STATUS and s.get("SeatNo") not in sold,
        })
    return seats


def is_normal_seat(seat, rule):
    allowed = rule.get("seat_kinds") or []
    return not allowed or seat["kind"] in allowed


def describe_seat(seat):
    return seat["kind"]
