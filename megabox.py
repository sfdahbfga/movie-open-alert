# 메가박스 조회 기능 (원본 megabox-open-push와 같은 API 사용)
import html
from datetime import datetime
from common import USER_AGENT, http_json

URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://www.megabox.co.kr",
    "Referer": "https://www.megabox.co.kr/booking",
    "User-Agent": USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
}


def _request(brch_no="", area_cd="", special_cd="", movie_no="", play_de=""):
    json_data = {
        "arrMovieNo": movie_no,
        "playDe": play_de or datetime.now().strftime("%Y%m%d"),
        "brchNoListCnt": 1 if brch_no else 0,
        "brchNo1": brch_no,
        "areaCd1": special_cd or area_cd,
        "spclbYn1": ("Y" if special_cd else "N") if brch_no else "",
        "theabKindCd1": special_cd,
        "brchAll": "",
        "brchSpcl": special_cd,
        "movieNo1": movie_no,
        "sellChnlCd": "",
    }
    return http_json(URL, json_body=json_data, headers=HEADERS)


def get_all_theaters():
    """전체 지점 + 특별관 목록"""
    data = _request()
    theaters = []
    for b in data.get("areaBrchList") or []:
        theaters.append({
            "brch_no": b["brchNo"], "brch_nm": html.unescape(b["brchNm"]),
            "area_cd": b["areaCd"], "special_cd": "", "special_nm": "",
        })
    for b in data.get("spclbBrchList") or []:
        theaters.append({
            "brch_no": b["brchNo"], "brch_nm": html.unescape(b["brchNm"]),
            "area_cd": b.get("oriAreaCd", ""), "special_cd": b["areaCd"],
            "special_nm": html.unescape(b.get("areaCdNm", "")),
        })
    return theaters


def search_theaters(keyword):
    keyword = keyword.replace(" ", "")
    return [t for t in get_all_theaters() if keyword in t["brch_nm"].replace(" ", "")]


def search_movies(keyword):
    """영화 이름으로 영화번호 찾기 (개봉 예정작 포함) -> [(영화번호, 제목), ...]"""
    keyword = keyword.replace(" ", "")
    result = {}
    for m in _request().get("movieList") or []:
        name = html.unescape(m.get("movieNm", ""))
        if keyword in name.replace(" ", ""):
            result[m["movieNo"]] = name
    return list(result.items())


def fetch_showings(target):
    """감시 대상의 '예매 가능한 회차' 집합 ('날짜|시작시간|상영관|영화|형식')"""
    args = (target["brch_no"], target.get("area_cd", ""), target.get("special_cd", ""), target.get("movie_no", ""))
    first = _request(*args)
    dates = [item["playDe"] for item in (first.get("movieFormDeList") or []) if item.get("formAt", "Y") == "Y"]
    result = set()
    for play_de in dates:
        data = first if play_de == ((first.get("paramMap") or {}) if isinstance(first.get("paramMap"), dict) else {}).get("playDe") else _request(*args, play_de=play_de)
        for row in data.get("movieFormList") or []:
            if row.get("playDe") != play_de:
                continue
            result.add("|".join([
                play_de,
                str(row.get("playStartTime") or ""),
                html.unescape(str(row.get("theabExpoNm") or "")),
                html.unescape(str(row.get("movieNm") or "")),
                html.unescape(str(row.get("playKindNm") or "")),
            ]))
    return result


##################################################
# 빈자리 감시용 (로그인 없이 동작하는 것을 2026-11에 확인한 API만 사용)

SEAT_URL = "https://www.megabox.co.kr/on/oh/ohz/PcntSeatChoi/selectSeatList.do"
BOOKING_URL = "https://www.megabox.co.kr/booking"
# 회차 키워드를 찾아볼 필드들 ('아트그라피'는 movieNm과 movieCttsNm에 들어 있음)
SHOWING_TEXT_FIELDS = ("movieNm", "rpstMovieNm", "movieCttsNm", "theabExpoNm", "playKindNm",
                       "playTyCdNm", "eventDivCdNm")


def _hhmm(t):
    t = str(t or "").replace(":", "")
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t


def list_seat_showings(watch):
    """1단계: 조건에 맞는 회차와 '잔여석 숫자' 목록 (회차 목록 API만 사용, 가벼움)"""
    brch_no, area_cd = watch["brch_no"], watch.get("area_cd", "")
    wanted_dates = set(watch.get("dates") or [])
    movie_kw = watch.get("movie_keyword", "").replace(" ", "")
    showing_kw = watch.get("showing_keyword", "").replace(" ", "")
    times = set(watch.get("times") or [])
    first = _request(brch_no, area_cd)
    dates = [d["playDe"] for d in (first.get("movieFormDeList") or []) if d.get("formAt", "Y") == "Y"]
    result = []
    for play_de in dates:
        if wanted_dates and play_de not in wanted_dates:
            continue
        data = _request(brch_no, area_cd, play_de=play_de)
        for row in data.get("movieFormList") or []:
            if row.get("playDe") != play_de:
                continue
            movie = html.unescape(str(row.get("movieNm") or ""))
            movie_text = (movie + html.unescape(str(row.get("rpstMovieNm") or ""))).replace(" ", "")
            if movie_kw and movie_kw not in movie_text:
                continue
            all_text = " ".join(html.unescape(str(row.get(k) or "")) for k in SHOWING_TEXT_FIELDS).replace(" ", "")
            if showing_kw and showing_kw not in all_text:
                continue
            time_ = _hhmm(row.get("playStartTime"))
            if times and time_ not in times:
                continue
            if row.get("bokdAbleAt") == "N":
                continue  # 예매 불가 회차
            result.append({
                "key": f"megabox|{brch_no}|{row.get('playSchdlNo')}",
                "date": play_de, "time": time_,
                "theater": "메가박스 " + html.unescape(str(row.get("brchNm") or brch_no)),
                "screen": html.unescape(str(row.get("theabExpoNm") or "")),
                "movie": movie,
                "rest": int(row.get("restSeatCnt") or 0),
                "seat_args": {"playSchdlNo": row.get("playSchdlNo"), "brchNo": brch_no},
            })
    return result


def get_seats(seat_args):
    """2단계: 좌석 배치도 -> [{name, cls, zone, free}]"""
    data = http_json(SEAT_URL, json_body=seat_args, headers=HEADERS)
    seats = []
    for s in data.get("seatListSD01") or []:
        if s.get("seatExpoAt") == "N" or not s.get("rowNm"):
            continue
        seats.append({
            "name": f"{s.get('rowNm')}{s.get('seatNo')}",
            "cls": s.get("seatClassCd") or "",
            "zone": s.get("seatZoneCd") or "",
            # 빈 좌석은 상태(seatStatCd)가 판매유형(seatSellTyCd)과 같고, 팔린 좌석은 SCT04 등으로 옴
            "free": bool(s.get("seatStatCd")) and s.get("seatStatCd") == s.get("seatSellTyCd"),
        })
    return seats


def is_normal_seat(seat, rule):
    """설정(seat_filter.megabox)의 값과 비교해 '일반석'인지"""
    for field, key in (("cls", "seat_classes"), ("zone", "seat_zones")):
        allowed = rule.get(key) or []
        if allowed and seat[field] not in allowed:
            return False
    return True


def describe_seat(seat):
    return f"{seat['cls']}/{seat['zone']}"
