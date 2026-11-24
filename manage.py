# 영화관 추가/삭제/목록, 디스코드 설정 메뉴
import os
import shutil
import subprocess
from datetime import datetime

import cgv
import megabox
import lotte
import coupons
from common import BASE_DIR, load_config, save_config, send_discord, get_webhook_url, set_webhook_url, pretty_date
from server_script import build_cloud_init, build_phone_script

CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스", "lottecinema": "롯데시네마"}


class InputCancelled(Exception):
    """입력 중에 0을 넣으면 지금 하던 추가/설정을 통째로 취소"""


def ask(prompt):
    s = input(prompt).strip()
    if s == "0":
        raise InputCancelled()
    return s


def choose(items, label):
    """번호 목록에서 하나 고르기. 취소하면 None"""
    if not items:
        print("  검색 결과가 없습니다.")
        return None
    for i, text in enumerate(items, 1):
        print(f"  {i}. {label(text)}")
    while True:
        s = ask("번호 선택 (취소: 엔터) > ")
        if s == "":
            return None
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]
        print("  목록에 있는 번호를 입력하세요.")


def add_cgv(config):
    keyword = ask("\nCGV 극장 이름 검색 (예: 용산) > ")
    if not keyword:
        return
    picked = choose(cgv.search_theaters(keyword), lambda t: f"{t[1]}  (극장번호 {t[0]})")
    if not picked:
        return
    site_no, site_nm = picked

    print("\n이 극장의 상영관 목록을 불러오는 중...")
    try:
        names = cgv.screen_names(site_no)
        for n in names:
            print("   ·", n)
    except Exception as e:
        print("  상영관 목록을 불러오지 못했습니다:", e)
    print("\n특정 상영관만 감시하려면 상영관 이름에 들어있는 단어를 입력하세요.")
    print("  예) IMAX, 4DX, SCREENX, SPHEREX, 리클라이너  /  극장 전체를 감시하려면 그냥 엔터")
    screen_keyword = ask("상영관 키워드 > ")
    print("\n특정 영화만 감시하려면 제목 일부를 입력하세요. (모든 영화: 그냥 엔터)")
    movie_keyword = ask("영화 제목 키워드 > ")

    name = " ".join(x for x in ["CGV", site_nm.replace("CGV", "").strip(), screen_keyword, movie_keyword] if x)
    target = {"name": name, "chain": "cgv", "site_no": site_no,
              "screen_keyword": screen_keyword, "movie_keyword": movie_keyword}
    finish_add(config, target)


def add_megabox(config):
    keyword = ask("\n메가박스 지점 이름 검색 (예: 코엑스) > ")
    if not keyword:
        return

    def label(t):
        hall = f" [{t['special_nm']}]" if t["special_cd"] else " [극장 전체]"
        return f"{t['brch_nm']}{hall}  (지점번호 {t['brch_no']})"

    picked = choose(megabox.search_theaters(keyword), label)
    if not picked:
        return

    print("\n특정 영화만 감시하려면 제목 일부를 입력하세요. (모든 영화: 그냥 엔터)")
    movie_keyword = ask("영화 제목 키워드 > ")
    movie_no, movie_nm = "", ""
    if movie_keyword:
        movie = choose(megabox.search_movies(movie_keyword), lambda m: m[1])
        if not movie:
            print("  영화를 고르지 않아서 모든 영화를 감시합니다.")
        else:
            movie_no, movie_nm = movie

    name = " ".join(x for x in ["메가박스", picked["brch_nm"], picked["special_nm"], movie_nm] if x)
    target = {"name": name, "chain": "megabox", "brch_no": picked["brch_no"],
              "area_cd": picked["area_cd"], "special_cd": picked["special_cd"],
              "movie_no": movie_no, "movie_name": movie_nm}
    finish_add(config, target)


def _pick_lotte_theater():
    picked = choose(lotte.search_theaters(ask("\n롯데시네마 극장 이름 검색 (예: 천안) > ")),
                    lambda t: f"{t['name']}  (극장번호 {t['cinema_id']})")
    if not picked:
        return None
    return {"division": picked["division"], "detail": picked["detail"], "cinema_id": picked["cinema_id"]}, picked["name"]


def add_lotte(config):
    r = _pick_lotte_theater()
    if not r:
        return
    target, theater_name = r
    print("\n특정 상영관만 감시하려면 상영관 이름에 들어있는 단어를 입력하세요.")
    print("  예) 수퍼플렉스, 샤롯데, 리클라이너, 광음  /  극장 전체를 감시하려면 그냥 엔터")
    screen_keyword = ask("상영관 키워드 > ")
    print("\n특정 영화만 감시하려면 제목 일부를 입력하세요. (모든 영화: 그냥 엔터)")
    movie_keyword = ask("영화 제목 키워드 > ")
    name = " ".join(x for x in ["롯데시네마", theater_name, screen_keyword, movie_keyword] if x)
    target.update({"name": name, "chain": "lottecinema",
                   "screen_keyword": screen_keyword, "movie_keyword": movie_keyword})
    finish_add(config, target)


def finish_add(config, target):
    print("\n현재 예매가 열려 있는 회차를 확인하는 중...")
    try:
        fetch = {"cgv": cgv, "megabox": megabox, "lottecinema": lotte}[target["chain"]].fetch_showings
        showings = fetch(target)
        dates = sorted({x.split("|")[0] for x in showings})
        if showings:
            print(f"  → 회차 {len(showings)}개 / 날짜: " + ", ".join(pretty_date(d) for d in dates))
        else:
            print("  → 아직 열린 회차가 없음")
    except Exception as e:
        print("  확인 실패(추가는 가능):", e)

    new_name = ask(f"\n알림에 표시할 이름 (그대로 쓰려면 엔터) [{target['name']}] > ")
    if new_name:
        target["name"] = new_name
    config["targets"].append(target)
    save_config(config)
    push_config(f"예매 오픈 감시 추가: {target['name']}")
    print(f"✅ '{target['name']}' 추가 완료!")


def _all_entries(config):
    """예매 오픈 감시 + 빈자리 감시를 한 목록으로 (목록 보기/삭제용)"""
    entries = [("targets", i, f"[{CHAIN_NAME[t['chain']]} 예매 오픈] {t['name']}")
               for i, t in enumerate(config["targets"])]
    entries += [("seat_watches", i, f"[{CHAIN_NAME[w['chain']]} 빈자리] {w['name']}")
                for i, w in enumerate(config.get("seat_watches") or [])]
    entries += [("coupon_watches", i, f"[{CHAIN_NAME[w['chain']]} 쿠폰] {w['name']}")
                for i, w in enumerate(config.get("coupon_watches") or [])]
    return entries


def list_targets(config):
    print()
    entries = _all_entries(config)
    if not entries:
        print("  등록된 감시가 없습니다.")
    for n, (_, _, label) in enumerate(entries, 1):
        print(f"  {n}. {label}")


def remove_target(config):
    list_targets(config)
    entries = _all_entries(config)
    if not entries:
        return
    s = ask("삭제할 번호 (취소: 엔터) > ")
    if s.isdigit() and 1 <= int(s) <= len(entries):
        key, idx, label = entries[int(s) - 1]
        config[key].pop(idx)
        save_config(config)
        print(f"🗑  '{label}' 삭제했습니다.")
        push_config(f"감시 삭제: {label}")


def add_seat_watch(config):
    print("\n빈자리 감시를 추가합니다. (일반석이 비면 디스코드로 알림)")
    roles = config.get("device_roles") or {}
    print(f"  ※ 서버(오라클): {', '.join(CHAIN_NAME[c] for c in roles.get('server', []))}  /  "
          f"휴대폰: {', '.join(CHAIN_NAME[c] for c in roles.get('phone', []))}")
    chain = ask("극장 체인 — 1: CGV, 2: 메가박스, 3: 롯데시네마 > ")
    if chain not in ("1", "2", "3"):
        return
    if chain == "3":
        r = _pick_lotte_theater()
        if not r:
            return
        watch, theater_name = r
        watch["chain"] = "lottecinema"
        theater_name = "롯데시네마 " + theater_name
    elif chain == "1":
        picked = choose(cgv.search_theaters(ask("\nCGV 극장 이름 검색 (예: 용산) > ")),
                        lambda t: f"{t[1]}  (극장번호 {t[0]})")
        if not picked:
            return
        watch = {"chain": "cgv", "site_no": picked[0]}
        theater_name = "CGV " + picked[1].replace("CGV", "").strip()
    else:
        theaters = [t for t in megabox.search_theaters(ask("\n메가박스 지점 이름 검색 (예: 수원AK) > "))
                    if not t["special_cd"]]
        picked = choose(theaters, lambda t: f"{t['brch_nm']}  (지점번호 {t['brch_no']})")
        if not picked:
            return
        watch = {"chain": "megabox", "brch_no": picked["brch_no"], "area_cd": picked["area_cd"]}
        theater_name = "메가박스 " + picked["brch_nm"]

    print("\n영화 제목 일부를 입력하세요. (예: 룩백)  모든 영화: 그냥 엔터")
    watch["movie_keyword"] = ask("영화 키워드 > ")
    print("\n특정 회차만 보려면 키워드를 입력하세요. (예: 아트그라피, IMAX, 자막)  모든 회차: 그냥 엔터")
    print("  ※ 영화 이름·상영관 이름·회차 속성(메가박스 '콘텐트 구분' 등)에서 찾습니다.")
    watch["showing_keyword"] = ask("회차 키워드 > ")
    print("\n날짜를 입력하세요. 여러 개는 쉼표로. (예: 1122  또는  20261122,20261123)  예매 열린 모든 날짜: 그냥 엔터")
    dates = []
    for d in ask("날짜 > ").replace(" ", "").split(","):
        if not d:
            continue
        if len(d) == 4 and d.isdigit():
            d = datetime.now().strftime("%Y") + d
        if len(d) != 8 or not d.isdigit():
            print(f"  '{d}'는 날짜 형식이 아니라서 건너뜁니다.")
            continue
        dates.append(d)
    watch["dates"] = dates
    print("\n시작 시간을 정하려면 입력하세요. 여러 개는 쉼표로. (예: 11:00,14:30)  모든 시간: 그냥 엔터")
    watch["times"] = [t.strip() for t in ask("시간 > ").split(",") if t.strip()]

    print("\n조건에 맞는 회차를 확인하는 중... (회차 목록만 조회)")
    try:
        site = {"cgv": cgv, "megabox": megabox, "lottecinema": lotte}[watch["chain"]]
        showings = site.list_seat_showings(watch)
        if not showings:
            print("  → 지금은 조건에 맞는 회차가 없어요. (예매가 아직 안 열렸으면 열린 뒤부터 감시돼요)")
        for sh in showings[:15]:
            print(f"  · {pretty_date(sh['date'])} {sh['time']} {sh['screen']} «{sh['movie']}» 잔여 {sh['rest']}석")
        if len(showings) > 15:
            print(f"  … 외 {len(showings) - 15}개")
        if len(showings) > 10:
            print("  ⚠ 회차가 많으면 처음 시작할 때 좌석 확인 요청이 많아져요. 날짜나 키워드로 좁히는 걸 추천해요.")
    except Exception as e:
        print("  확인 실패(추가는 가능):", e)

    default_name = " ".join(x for x in [theater_name, watch["movie_keyword"], watch["showing_keyword"],
                                        ",".join(d[4:] for d in dates)] if x)
    new_name = ask(f"\n알림에 표시할 이름 (그대로 쓰려면 엔터) [{default_name}] > ")
    watch["name"] = new_name or default_name
    config.setdefault("seat_watches", []).append(watch)
    save_config(config)
    push_config(f"빈자리 감시 추가: {watch['name']}")
    print(f"✅ 빈자리 감시 '{watch['name']}' 추가 완료!")


def add_coupon_watch(config):
    print("\n선착순 할인 쿠폰 취소분 감시를 추가합니다.")
    print("  마감된 쿠폰이 취소로 다시 풀리면 디스코드로 알려 줘요. 쿠폰 받기는 앱에서 직접 하세요.")
    chain = {"1": "cgv", "2": "megabox", "3": "lottecinema"}.get(
        ask("극장 체인 — 1: CGV (서프라이즈·스피드 쿠폰), 2: 메가박스 (빵원티켓), 3: 롯데시네마 (무비싸다구) > "))
    if not chain:
        return
    print(f"\n{CHAIN_NAME[chain]}에서 진행 중인 쿠폰을 모두 불러오는 중... (이벤트가 많으면 조금 걸려요)")
    try:
        rows = coupons.all_active_coupons(chain)
    except Exception as e:
        print("  목록을 불러오지 못했어요:", e)
        rows = []
    if rows:
        print(f"\n진행 중인 쿠폰 전체 ({len(rows)}개) — 종류별, 시작일 빠른 순")
        last_section = last_event = None
        for i, (section, no, event_name, period, c, err) in enumerate(rows, 1):
            if section != last_section:
                print(f"\n━━━━━━━━ {section} ━━━━━━━━")
                last_section, last_event = section, None
            if no != last_event:
                print(f"\n  ■ {event_name}" + (f"   ({coupons.short_period(period)})" if period else ""))
                last_event = no
            print(f"     {i:>2}. " + (coupons.describe(c) if c else f"(이벤트 전체) — {err}"))
    else:
        print("  지금 진행 중인 쿠폰을 찾지 못했어요.")
    print("\n감시할 번호를 쉼표로 입력하세요. (예: 1,4,5)")
    print("  목록에 없는 이벤트는 이벤트 페이지 주소를 붙여넣으세요.")
    s = ask("번호 또는 주소 (취소: 엔터) > ")
    if not s:
        return

    # 고른 것들을 이벤트별로 묶음 -> 이벤트 하나당 감시 하나
    picked = {}   # 이벤트번호 -> (이벤트이름, [쿠폰키...] 또는 None=전체)
    if s.replace(",", "").replace(" ", "").isdigit():
        for n in s.replace(" ", "").split(","):
            if not n.isdigit() or not 1 <= int(n) <= len(rows):
                print(f"  '{n}'번은 목록에 없어서 건너뜁니다.")
                continue
            _, no, event_name, _, c, _ = rows[int(n) - 1]
            name, keys = picked.get(no, (event_name, []))
            if c is None or keys is None:
                keys = None          # 이벤트 전체
            else:
                keys.append(c["key"])
            picked[no] = (name, keys)
    else:
        link_chain, no = coupons.parse_event_link(s)
        if not link_chain:
            print("  주소에서 이벤트 번호를 찾지 못했어요. (메가박스 eventNo=, CGV evntNo=, 롯데시네마 eventId= 가 들어간 주소)")
            return
        if link_chain != chain:
            print(f"  {CHAIN_NAME[link_chain]} 주소라서 {CHAIN_NAME[link_chain]} 감시로 추가할게요.")
            chain = link_chain
        try:
            event_name, found = coupons.list_coupons({"chain": chain, "event_no": no})
        except Exception as e:
            print("  확인 실패:", e)
            return
        print(f"\n[{event_name}]")
        for i, c in enumerate(found, 1):
            print(f"  {i}. {coupons.describe(c)}")
        keys = None
        if len(found) > 1:
            nums = [x for x in ask("감시할 쿠폰 번호 (쉼표로, 전부: 엔터) > ").replace(" ", "").split(",") if x]
            keys = [found[int(x) - 1]["key"] for x in nums if x.isdigit() and 1 <= int(x) <= len(found)] or None
        picked[no] = (event_name, keys)
    if not picked:
        return

    added = []
    for no, (event_name, keys) in picked.items():
        watch = {"chain": chain, "event_no": no, "picks": keys or []}
        default_name = f"{CHAIN_NAME[chain]} {event_name}".strip()
        new_name = ask(f"\n알림에 표시할 이름 (그대로 쓰려면 엔터) [{default_name}] > ")
        watch["name"] = new_name or default_name
        config.setdefault("coupon_watches", []).append(watch)
        added.append(watch["name"])
    save_config(config)
    push_config("쿠폰 감시 추가: " + ", ".join(added))
    roles = config.get("device_roles") or {}
    where = "휴대폰이" if chain in roles.get("phone", []) else "오라클 서버가"
    for n in added:
        print(f"✅ 쿠폰 감시 '{n}' 추가 완료!")
    print(f"   ({where} 5분 안에 받아서 시작해요)")

def set_webhook(config):
    print("\n디스코드 채널 설정 > 연동 > 웹후크 > 새 웹후크 > '웹후크 URL 복사' 로 얻은 주소를 붙여넣으세요.")
    print("(secret.json에 저장됩니다. 이 주소를 아는 사람은 누구나 채널에 메시지를 보낼 수 있으니 공유하지 마세요.)")
    print(f"현재 값: {'설정됨' if get_webhook_url() else '(없음)'}")
    url = ask("웹후크 URL (변경 안 함: 엔터) > ")
    if url:
        if not url.startswith("https://") or "/api/webhooks/" not in url:
            print("디스코드 웹후크 주소 형식이 아닌 것 같아요. 'https://discord.com/api/webhooks/...' 형태인지 확인하세요.")
            return
        set_webhook_url(url)
        print("저장했습니다. '테스트 알림 보내기'로 확인해 보세요.")


def test_message(config):
    try:
        if send_discord("🔔 예매 오픈 알리미 테스트 메시지입니다."):
            print("디스코드로 테스트 메시지를 보냈습니다. 휴대폰에 알림이 왔는지 확인하세요.")
    except Exception as e:
        print("전송 실패:", e)


def make_server_script(config):
    if not get_webhook_url():
        print("먼저 8번 메뉴에서 디스코드 웹후크를 설정하세요.")
        return
    try:
        path, size, targets = build_cloud_init()
    except RuntimeError as e:
        print(e)
        return
    print(f"\n✅ 오라클 서버 설치 파일을 만들었습니다: {path}")
    chains = (config.get("device_roles") or {}).get("server", [])
    print(f"   맡는 체인: {', '.join(CHAIN_NAME[c] for c in chains)}  (CGV는 클라우드 서버 접속을 막아서 휴대폰에서 돌립니다)")
    for t in targets:
        print("    ·", ("[빈자리] " if "showing_keyword" in t else "[예매 오픈] ") + t["name"])
    print("   오라클에서 서버(인스턴스)를 만들 때 'cloud-init 스크립트' 파일로 넣으면 됩니다.")
    print("   ※ 웹후크 주소가 들어 있으니 이 파일은 다른 사람에게 보내지 마세요.")


def make_phone_script(config):
    if not get_webhook_url():
        print("먼저 8번 메뉴에서 디스코드 웹후크를 설정하세요.")
        return
    try:
        path, size, targets = build_phone_script()
    except RuntimeError as e:
        print(e)
        return
    print(f"\n✅ 휴대폰(Termux) 설치 파일을 만들었습니다: {path}")
    chains = (config.get("device_roles") or {}).get("phone", [])
    print(f"   맡는 체인: {', '.join(CHAIN_NAME[c] for c in chains)}")
    for t in targets:
        print("    ·", ("[빈자리] " if "showing_keyword" in t else "[예매 오픈] ") + t["name"])
    print("   이 파일을 휴대폰의 '다운로드(Download)' 폴더로 옮긴 뒤 Termux에서 실행하세요.")
    print("   ※ 웹후크 주소가 들어 있으니 이 파일은 다른 사람에게 보내지 마세요.")

##################################################
# GitHub 저장소 연동 (PC 폴더가 저장소를 clone한 폴더일 때만 동작)

def _git(*args):
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8")


def is_git_repo():
    return shutil.which("git") is not None and os.path.isdir(os.path.join(BASE_DIR, ".git"))


def git_pull():
    if not is_git_repo():
        return
    r = _git("pull", "--ff-only")
    if r.returncode != 0:
        print("⚠ 저장소의 최신 내용을 받지 못했어요:", (r.stderr or r.stdout).strip()[:300])


def push_config(message):
    """config.json을 저장소에 올림 → 서버/휴대폰이 5분 안에 자동 적용"""
    if not is_git_repo():
        print("ℹ 이 폴더는 GitHub 저장소와 연결돼 있지 않아서 자동으로 올리지 못했어요.")
        print("  → Claude에게 채팅으로 부탁하거나, GitHub 웹사이트에서 config.json을 고치면 반영돼요.")
        return
    _git("add", "config.json")
    r = _git("commit", "-m", message)
    if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
        print("ℹ 올릴 변경 사항이 없어요.")
        return
    r = _git("push")
    if r.returncode == 0:
        print("☁ GitHub에 올렸어요. 서버와 휴대폰에 5분 안에 자동으로 적용돼요.")
    else:
        print("⚠ GitHub에 올리지 못했어요:", (r.stderr or r.stdout).strip()[:300])


def manual_push(config):
    push_config("감시 목록 수정")


def main():
    git_pull()
    config = load_config()
    menu = [
        ("CGV 영화관 추가", add_cgv),
        ("메가박스 영화관 추가", add_megabox),
        ("롯데시네마 영화관 추가", add_lotte),
        ("빈자리 감시 추가", add_seat_watch),
        ("쿠폰 취소분 감시 추가", add_coupon_watch),
        ("등록된 감시 목록", list_targets),
        ("감시 삭제", remove_target),
        ("디스코드 웹후크 설정", set_webhook),
        ("테스트 알림 보내기", test_message),
        ("오라클 서버 설치 파일 만들기", make_server_script),
        ("휴대폰 설치 파일 만들기", make_phone_script),
        ("GitHub에 지금 올리기", manual_push),
    ]
    while True:
        print("\n===== 예매 오픈 알리미 설정 =====")
        for i, (title, _) in enumerate(menu, 1):
            print(f" {i}. {title}")
        print(" 0. 종료")
        s = input("메뉴 번호 > ").strip()
        if s == "0":
            break
        if s.isdigit() and 1 <= int(s) <= len(menu):
            print("  ※ 입력하다가 0을 입력하면 언제든 취소하고 메뉴로 돌아가요.")
            try:
                menu[int(s) - 1][1](config)
            except InputCancelled:
                print("\n↩ 입력을 취소했어요. 아무것도 저장하지 않았습니다.")
                config = load_config()   # 중간에 바뀐 내용이 남지 않게 저장된 설정으로 되돌림
            except Exception as e:
                print("오류가 발생했습니다:", e)


if __name__ == "__main__":
    main()
