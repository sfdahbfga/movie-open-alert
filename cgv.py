# CGV 조회 기능
# 원본 cgv-open-push(v1)는 옛 주소(ticket.cgv.co.kr)와 암호화된 극장 코드를 썼는데,
# CGV 리뉴얼 이후 사이트는 아래 새 API(극장번호가 '0110' 같은 평문)를 사용합니다.
from common import USER_AGENT, http_json

API = "https://cgv.co.kr/api/v1"
CO_CD = "A420"  # CGV 회사 코드 (사이트가 항상 이 값을 보냄)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://cgv.co.kr/cnm/movieBook/cinema",
    "User-Agent": USER_AGENT,
}


def _get(path, params):
    data = http_json(API + path, params=params, headers=HEADERS)
    if data.get("statusCode") not in (0, "0", None):
        raise RuntimeError(f"CGV 응답 오류: {data.get('statusMessage')}")
    return data.get("data")


def _find_sites(obj, found):
    # 응답 안에서 {siteNo, siteNm} 묶음을 모두 찾아냄
    if isinstance(obj, dict):
        if "siteNo" in obj and "siteNm" in obj:
            found[obj["siteNo"]] = obj["siteNm"]
        for v in obj.values():
            _find_sites(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _find_sites(v, found)


def get_all_theaters():
    """전체 CGV 극장 목록 -> [(극장번호, 극장이름), ...]"""
    data = _get("/content/site/searchAllRegionAndSite", {"coCd": CO_CD})
    found = {}
    _find_sites(data, found)
    return sorted(found.items(), key=lambda x: x[1])


def search_theaters(keyword):
    keyword = keyword.replace(" ", "")
    return [(no, nm) for no, nm in get_all_theaters() if keyword in nm.replace(" ", "")]


def get_open_dates(site_no):
    """예매가 열려 있는 날짜 목록 -> ['20261114', ...]"""
    data = _get("/booking/searchSiteScnscYmdListBySite", {"coCd": CO_CD, "siteNo": site_no}) or []
    return sorted({item["scnYmd"] for item in data if item.get("scnYmd")})


def get_schedule(site_no, ymd):
    """특정 날짜의 상영 시간표 (상영관, 영화 정보 포함)"""
    return _get("/booking/searchMovScnInfo",
                {"coCd": CO_CD, "siteNo": site_no, "scnYmd": ymd, "rtctlScopCd": "08"}) or []


def screen_names(site_no, max_days=3):
    """이 극장에 있는 상영관 이름들 (특별관 키워드 고를 때 참고용)"""
    names = set()
    for ymd in get_open_dates(site_no)[:max_days]:
        for row in get_schedule(site_no, ymd):
            names.add(row.get("expoScnsNm") or row.get("scnsNm") or "")
    return sorted(n for n in names if n)


def _match(row, screen_keyword, movie_keyword):
    if screen_keyword:
        text = " ".join(str(row.get(k) or "") for k in ("scnsNm", "expoScnsNm", "scnsEnm", "movkndDsplNm")).upper()
        if screen_keyword.upper().replace(" ", "") not in text.replace(" ", ""):
            return False
    if movie_keyword:
        text = " ".join(str(row.get(k) or "") for k in ("movNm", "prodNm", "expoProdNm", "movEnm")).replace(" ", "")
        if movie_keyword.replace(" ", "") not in text:
            return False
    return True


def _hhmm(t):
    t = str(t or "")
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 and t.isdigit() else t


def fetch_showings(target):
    """감시 대상의 '예매 가능한 회차' 집합을 돌려줌
    각 회차는 '날짜|시작시간|상영관|영화|형식' 문자열. 새 회차가 생기면 알림."""
    site_no = target["site_no"]
    screen_keyword = target.get("screen_keyword", "")
    movie_keyword = target.get("movie_keyword", "")
    result = set()
    for ymd in get_open_dates(site_no):
        for row in get_schedule(site_no, ymd):
            if not _match(row, screen_keyword, movie_keyword):
                continue
            result.add("|".join([
                str(row.get("scnYmd") or ymd),
                _hhmm(row.get("scnsrtTm")),
                str(row.get("expoScnsNm") or row.get("scnsNm") or ""),
                str(row.get("movNm") or row.get("prodNm") or ""),
                str(row.get("movkndDsplNm") or ""),
            ]))
    return result


##################################################
# 빈자리 감시용 (로그인 없이 동작하는 것을 2026-11에 확인한 API만 사용)

BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"
# 회차 키워드를 찾아볼 필드들
SHOWING_TEXT_FIELDS = ("movNm", "prodNm", "expoProdNm", "scnsNm", "expoScnsNm", "movkndDsplNm",
                       "sbtdivNm", "prmddNm", "videoAddexpCdNm", "salsTznNm")


def list_seat_showings(watch):
    """1단계: 조건에 맞는 회차와 '잔여석 숫자' 목록 (회차 목록 API만 사용, 가벼움)"""
    site_no = watch["site_no"]
    wanted_dates = set(watch.get("dates") or [])
    movie_kw = watch.get("movie_keyword", "").replace(" ", "")
    showing_kw = watch.get("showing_keyword", "").replace(" ", "")
    times = set(watch.get("times") or [])
    result = []
    for ymd in get_open_dates(site_no):
        if wanted_dates and ymd not in wanted_dates:
            continue  # 설정한 날짜만 시간표를 받음 (요청 수 절약)
        for row in get_schedule(site_no, ymd):
            movie_text = " ".join(str(row.get(k) or "") for k in ("movNm", "prodNm", "expoProdNm")).replace(" ", "")
            if movie_kw and movie_kw not in movie_text:
                continue
            all_text = " ".join(str(row.get(k) or "") for k in SHOWING_TEXT_FIELDS).replace(" ", "")
            if showing_kw and showing_kw not in all_text:
                continue
            time_ = _hhmm(row.get("scnsrtTm"))
            if times and time_ not in times:
                continue
            try:
                rest = int(row.get("frSeatCnt") or 0)
            except ValueError:
                rest = 0
            result.append({
                "key": f"cgv|{site_no}|{ymd}|{row.get('scnsNo')}|{row.get('scnSseq')}",
                "date": ymd, "time": time_,
                "theater": str(row.get("siteNm") or site_no),
                "screen": str(row.get("expoScnsNm") or row.get("scnsNm") or ""),
                "movie": str(row.get("movNm") or row.get("prodNm") or ""),
                "rest": rest,
                "seat_args": {"siteNo": site_no, "scnYmd": ymd,
                              "scnsNo": row.get("scnsNo"), "scnSseq": row.get("scnSseq")},
            })
    return result


def get_seats(seat_args):
    """2단계: 좌석 배치도 -> [{name, kind, sale_form, zone, free}]"""
    data = _get("/booking/searchIfSeatData", {"coCd": CO_CD, **seat_args}) or {}
    seats = []
    for item in data.get("items") or []:
        for s in item.get("seats") or []:
            seats.append({
                "name": f"{s.get('seatRowNm') or ''}{s.get('seatNo') or ''}",
                "kind": s.get("stkndNm") or "",
                "sale_form": next((f.get("seatSalfrmNm") for f in item.get("salfrms") or []
                                   if f.get("seatSalfrmCd") == s.get("seatSalfrmCd")), s.get("seatSalfrmCd") or ""),
                "zone": s.get("szoneKindNm") or "",
                "free": s.get("seatSaleYn") == "Y",
            })
    return seats


def is_normal_seat(seat, rule):
    """설정(seat_filter.cgv)의 값과 비교해 '일반석'인지"""
    for field, key in (("kind", "seat_kinds"), ("sale_form", "sale_forms"), ("zone", "zones")):
        allowed = rule.get(key) or []
        if allowed and seat[field] not in allowed:
            return False
    return True


def describe_seat(seat):
    return f"{seat['kind']}/{seat['sale_form']}/{seat['zone']}"
