# 선착순 할인 쿠폰(빵원티켓 / 서프라이즈·스피드 쿠폰 / 무비싸다구) 취소분 감시
# - 로그인 없이 보이는 정보만 사용 (쿠폰 받기, 예매는 하지 않음)
# - '마감 → 다시 받을 수 있음'으로 바뀌면 디스코드로 알림
import json
import os
import re
import time
from datetime import datetime

from common import (BASE_DIR, USER_AGENT, http_json, http_request, load_json, save_json,
                    save_log_info, save_log_error)

STATE_PATH = os.path.join(BASE_DIR, "coupon_state.json")
CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스", "lottecinema": "롯데시네마"}


def _parse_dt(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except (TypeError, ValueError):
            pass
    return None


def _won(n):
    try:
        return f"{int(n):,}원"
    except (TypeError, ValueError):
        return str(n)


##################################################
# CGV 서프라이즈 / 스피드 쿠폰
# 이벤트 정보 하나에 '사용된 수량(evtSprsSpeedCpnCnt)'과 구간별 전체 수량(fcfsEndSeq)이 들어 있음

CGV_API = "https://cgv.co.kr/api/v1/content/event/evt"
CGV_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://cgv.co.kr/",
    "User-Agent": USER_AGENT,
}


def cgv_event_url(event_no):
    return f"https://cgv.co.kr/evt/eventDetail?evntNo={event_no}"


def cgv_detail(event_no):
    data = http_json(f"{CGV_API}/evtDtl/searchEvtDtl", headers=CGV_HEADERS,
                     params={"coCd": "A420", "evntNo": event_no, "expoChnlCd": "01",
                             "previewYn": "N", "expnYn": "N"})
    if data.get("statusCode") not in (0, "0") or not data.get("data"):
        raise RuntimeError(f"CGV 이벤트 조회 오류: {data.get('statusMessage')}")
    return data["data"]


def cgv_active_events():
    """지금 진행 중인 서프라이즈/스피드 쿠폰 이벤트 -> [(이벤트번호, 영화이름)]"""
    data = http_json(f"{CGV_API}/evtSprsSpeed/searchSprsSpeedEvtMovPostr", headers=CGV_HEADERS,
                     params={"coCd": "A420"})
    return [(e["evntNo"], e.get("movNm") or "") for e in data.get("data") or []]


def cgv_coupons(watch):
    d = cgv_detail(watch["event_no"])
    start, end = _parse_dt(d.get("evntStartDt")), _parse_dt(d.get("evntEndDt"))
    now = datetime.now()
    active = (not start or now >= start) and (not end or now <= end)
    result = []
    for grp in ((d.get("evtRewrd") or {}).get("rewrdGrpLst") or []):
        for c in grp.get("evtRewrdDcList") or []:
            tiers = c.get("evtRewrdDcAplybssList") or []
            if not tiers:
                continue
            total = max(int(t.get("fcfsEndSeq") or 0) for t in tiers)
            used = int(c.get("evtSprsSpeedCpnCnt") or 0)
            # 다음 사람이 받게 될 구간의 할인 금액
            nxt = next((t for t in tiers if int(t.get("fcfsStartSeq") or 0) <= used + 1 <= int(t.get("fcfsEndSeq") or 0)), tiers[-1])
            result.append({
                "key": str(c.get("dcNo")),
                "label": c.get("onlnExpoNm") or d.get("evntNm") or "쿠폰",
                "available": used < total,
                "remaining": max(0, total - used), "total": total,
                "price": f"{_won(nxt.get('dcAmt'))} 할인" if nxt.get("dcAmt") else "",
                "active": active,
            })
    return d.get("evntNm") or "", result


##################################################
# 메가박스 빵원티켓 / 빵원티켓 플러스
# 쿠폰 상태가 이벤트 페이지 HTML 안에 미리 채워져서 옴 (매진이면 'end' 표시 + '사용완료')

MEGA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "User-Agent": USER_AGENT,
}


def megabox_event_url(event_no):
    return f"https://www.megabox.co.kr/event/detail?eventNo={event_no}"


def _megabox_parse(html):
    title = re.search(r"<title>([^<]*)</title>", html)
    name = re.search(r'class="n1">([^<]+)</dd>', html)
    event_name = (name.group(1).strip() if name else "") or (title.group(1).strip() if title else "")
    m = re.search(r"var sbscTyCd = '(\w+)'", html)
    kind = m.group(1) if m else ""
    a = html.find("function fn_zecCponDraw")
    b = html.find("function fn_insertEventCdl", a)
    if a < 0 or b < 0:
        raise RuntimeError("메가박스 이벤트 페이지에서 쿠폰 정보를 찾지 못했어요 (빵원티켓 이벤트가 맞는지 확인)")
    draw = html[a:b]
    buttons = re.findall(r'id="btnInsertEvt_(\d+)_"[^>]*>\s*<span>([^<]+)</span>', html)
    coupons = []
    if kind == "ZECP":
        sec = draw[draw.find("sbscTyCd == 'ZECP'"):]
        parts = re.split(r"^\s*type = '(ty\d+)';", sec, flags=re.M)
        # parts = [앞부분, 'ty01', 내용, 'ty02', 내용, ...]
        for i in range(1, len(parts) - 1, 2):
            body = parts[i + 1]
            if "coupon " not in body:
                continue
            sold = "' end\"" in body or "사용완료" in body
            nums = re.search(r'<span>\s*([\d,]+)\s*</span>\s*/\s*([\d,]+)', body)
            coupons.append({"sold": sold, "nums": nums.groups() if nums else None})
    elif kind == "ZEC":
        sec = draw[draw.find("sbscTyCd == 'ZEC'"):draw.find("sbscTyCd == 'ZECP'")]
        lmt = re.search(r'var lmtCnt = "([\d,]*)"', sec)
        cnt = re.search(r'var cponCnt = "([\d,]*)"', sec)
        rate = re.search(r"_\.toNumber\('([\d.]*)'\)", sec)
        if not rate or rate.group(1) == "":
            raise RuntimeError("메가박스 쿠폰 수량을 읽지 못했어요")
        coupons.append({"sold": float(rate.group(1)) >= 100,
                        "nums": (cnt.group(1), lmt.group(1)) if cnt and lmt else None})
    else:
        raise RuntimeError("빵원티켓 형식의 이벤트가 아니에요")
    if not coupons:
        raise RuntimeError("메가박스 쿠폰 표시를 읽지 못했어요 (페이지 형식이 바뀌었을 수 있어요)")
    return event_name, kind, coupons, buttons


def megabox_coupons(watch):
    html = http_request(megabox_event_url(watch["event_no"]), headers=MEGA_HEADERS).decode("utf-8", "replace")
    event_name, kind, coupons, buttons = _megabox_parse(html)
    result = []
    for i, c in enumerate(coupons):
        label = buttons[i][1].strip() if i < len(buttons) else f"쿠폰 {i + 1}"
        # 페이지의 'N / 전체' 숫자가 사용 수량인지 남은 수량인지 확실하지 않아서 그대로 보여줌
        shown = f"페이지 표시 {c['nums'][0]} / {c['nums'][1]}" if c["nums"] else ""
        result.append({"key": f"{i + 1}", "label": label, "available": not c["sold"],
                       "remaining": None, "total": None, "price": shown, "active": True})
    return event_name, result


##################################################
# 롯데시네마 무비싸다구
# 회차(쿠폰 오픈 시각)별로 'CpnCurYn'이 1이면 받을 수 있음, 아니면 Sold Out

LOTTE_URL = "https://www.lottecinema.co.kr/LCWS/Event/EventData.aspx"
LOTTE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": "https://www.lottecinema.co.kr",
    "Referer": "https://www.lottecinema.co.kr/NLCMW/",
    "User-Agent": USER_AGENT,
}


def lotte_event_url(event_id):
    return f"https://www.lottecinema.co.kr/NLCMW/Event/EventTemplateSpeedMulti?eventId={event_id}"


def lotte_coupons(watch):
    param = {"MethodName": "GetSpeedEventDetailMulti", "channelType": "MW", "osType": "W",
             "osVersion": USER_AGENT, "EventID": "", "MainEventID": watch["event_no"]}
    data = http_json(LOTTE_URL, form={"paramList": json.dumps(param, ensure_ascii=False)}, headers=LOTTE_HEADERS)
    if str(data.get("IsOK")).lower() != "true" or not data.get("SpeedEventDetail"):
        raise RuntimeError(f"롯데시네마 이벤트 조회 오류: {data.get('ResultMessage')}")
    d = data["SpeedEventDetail"][0]
    use_type = str(d.get("ConfigDownOrUseCd")) != "1"   # 0: 사용 수량 기준, 1: 다운로드 수량 기준
    now = datetime.now()
    result = []
    for grp in d.get("ItemGroup") or []:
        items = grp.get("Items") or []
        if not items:
            continue
        first = items[0]
        start = _parse_dt(f"{first.get('ProgressStartDate')} {first.get('ProgressStartTime')}")
        end = _parse_dt(first.get("ProgressEndDate"))
        active = (not start or now >= start) and (not end or now.date() <= end.date())
        cur = next((it for it in items if str(it.get("CpnCurYn")) == "1"), None)
        remaining = total = None
        if cur:
            mx = cur.get("CpnUsableMaxCnt") if use_type else cur.get("CpnDownloadMaxCnt")
            done = cur.get("CpnUsedCnt") if use_type else cur.get("SpeedCpnDownCnt")
            try:
                total, remaining = int(mx), max(0, int(mx) - int(done))
            except (TypeError, ValueError):
                pass
        when = start.strftime("%m/%d %H시") if start else ""
        result.append({
            "key": str(grp.get("EventID")),
            "label": f"{first.get('MovieNm') or ''} ({when} 오픈)".strip(),
            "available": cur is not None,
            "remaining": remaining, "total": total,
            "price": f"{_won(cur.get('DisplayCouponName'))} 관람" if cur and str(cur.get("DisplayCouponName", "")).isdigit() else "",
            "active": active,
        })
    return d.get("EventName") or "", result


##################################################
# 공통

SITES = {
    "cgv": (cgv_coupons, cgv_event_url),
    "megabox": (megabox_coupons, megabox_event_url),
    "lottecinema": (lotte_coupons, lotte_event_url),
}


def parse_event_link(text):
    """붙여넣은 이벤트 주소 -> (체인, 이벤트번호)"""
    text = text.strip()
    for chain, pattern in (("cgv", r"cgv\.co\.kr.*[?&]evntNo=(\d+)"),
                           ("megabox", r"megabox\.co\.kr.*[?&]eventNo=(\d+)"),
                           ("lottecinema", r"lottecinema\.co\.kr.*[?&](?:eventId|eventID)=(\d+)")):
        m = re.search(pattern, text, re.I)
        if m:
            return chain, m.group(1)
    return None, None


def list_coupons(watch):
    fetch, _ = SITES[watch["chain"]]
    event_name, coupons = fetch(watch)
    picks = set(watch.get("picks") or [])
    if picks:
        coupons = [c for c in coupons if c["key"] in picks]
    return event_name, coupons


def describe(c):
    parts = []
    if c.get("price"):
        parts.append(c["price"])
    if c.get("remaining") is not None and c.get("total"):
        parts.append(f"남은 수량 {c['remaining']:,} / {c['total']:,}")
    if not c.get("active", True):
        parts.append("기간 아님")
    status = "받을 수 있음" if c["available"] else "마감"
    return f"{c['label']} — {status}" + (f" ({', '.join(parts)})" if parts else "")


class CouponWatcher:
    def __init__(self, watch, message_queue):
        self.watch = watch
        self.queue = message_queue
        self.state_id = watch["name"] + "|" + json.dumps(
            {k: watch.get(k) for k in ("chain", "event_no", "picks")}, sort_keys=True)
        saved = load_json(STATE_PATH, {}).get(self.state_id, {})
        self.last = {k: bool(v) for k, v in saved.items()}   # 쿠폰별 마지막 상태 (True = 받을 수 있음)

    def save(self):
        all_state = load_json(STATE_PATH, {})
        all_state[self.state_id] = self.last
        save_json(STATE_PATH, all_state)

    def run_once(self):
        _, coupons = list_coupons(self.watch)
        changed = False
        for c in coupons:
            if not c.get("active", True):
                continue   # 기간 밖이면 상태 기록도 안 함 (오픈 시각의 선착순은 취소분이 아니니까)
            before = self.last.get(c["key"])
            now = bool(c["available"])
            if before is False and now:
                self.queue.put(self.format_message(c))
                save_log_info(f"[쿠폰] {self.watch['name']}: 취소분 발생 — {describe(c)}", True)
            elif before is True and not now:
                save_log_info(f"[쿠폰] {self.watch['name']}: 다시 마감 — {c['label']}", True)
            if before != now:
                self.last[c["key"]] = now
                changed = True
        if changed:
            self.save()
        return coupons

    def format_message(self, c):
        _, url_of = SITES[self.watch["chain"]]
        lines = [f"🎟️ **쿠폰 취소분 알림** [{self.watch['name']}]",
                 f"쿠폰: {c['label']}",
                 "지금 다시 받을 수 있어요!"]
        if c.get("price"):
            lines.append(f"혜택: {c['price']}")
        if c.get("remaining") is not None and c.get("total"):
            lines.append(f"남은 수량: {c['remaining']:,} / {c['total']:,}")
        lines.append(f"이벤트: {url_of(self.watch['event_no'])}")
        lines.append("※ 쿠폰 받기와 예매는 앱에서 직접 해 주세요.")
        return "\n".join(lines)


def coupon_watch_main(watch, message_queue, status, interval, error_alert_count=10):
    name = watch["name"]
    watcher = CouponWatcher(watch, message_queue)
    error_count = 0
    first = True
    while True:
        try:
            coupons = watcher.run_once()
            if error_count >= error_alert_count:
                message_queue.put(f"✅ [{name}] 쿠폰 조회가 다시 정상화됐습니다.")
            error_count = 0
            open_cnt = sum(1 for c in coupons if c["available"] and c.get("active", True))
            status[name] = f"쿠폰 감시 정상 (쿠폰 {len(coupons)}개, 지금 받을 수 있는 것 {open_cnt}개)"
            if first:
                first = False
                save_log_info(f"[쿠폰] {name} 감시 시작: " + " / ".join(describe(c) for c in coupons), True)
            time.sleep(interval)
        except Exception as e:
            error_count += 1
            status[name] = f"쿠폰 조회 실패 {error_count}회 연속 ({e})"
            save_log_error(f"[쿠폰] {name} error ({error_count}회 연속) : {e}")
            if error_count == error_alert_count:
                message_queue.put(f"⚠️ [{name}] 쿠폰 조회가 {error_alert_count}번 연속 실패했습니다.\n"
                                  f"원인: {e}\n(이벤트가 끝났거나 사이트 구조가 바뀌었을 수 있어요)")
            time.sleep(min(60 * error_count, 600))
