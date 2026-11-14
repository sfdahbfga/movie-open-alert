# 영화관 추가/삭제/목록, 디스코드 설정 메뉴
import os
import shutil
import subprocess
from datetime import datetime

import cgv
import megabox
from common import BASE_DIR, load_config, save_config, send_discord, get_webhook_url, set_webhook_url, pretty_date
from server_script import build_cloud_init, build_phone_script

CHAIN_NAME = {"cgv": "CGV", "megabox": "메가박스"}


def ask(prompt):
    return input(prompt).strip()


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


def finish_add(config, target):
    print("\n현재 예매가 열려 있는 회차를 확인하는 중...")
    try:
        fetch = cgv.fetch_showings if target["chain"] == "cgv" else megabox.fetch_showings
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
    print("  ※ CGV는 휴대폰(Termux), 메가박스는 오라클 서버에서 돌아갑니다.")
    chain = ask("극장 체인 — 1: CGV, 2: 메가박스 > ")
    if chain not in ("1", "2"):
        return
    if chain == "1":
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
        site = cgv if watch["chain"] == "cgv" else megabox
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
        print("먼저 5번 메뉴에서 디스코드 웹후크를 설정하세요.")
        return
    try:
        path, size, targets = build_cloud_init()
    except RuntimeError as e:
        print(e, "(오라클 서버에는 메가박스 영화관만 들어갑니다)")
        return
    print(f"\n✅ 오라클 서버 설치 파일을 만들었습니다: {path}")
    print("   들어간 감시 (메가박스만 — CGV는 클라우드 서버 접속을 막아서 휴대폰에서 돌립니다):")
    for t in targets:
        print("    ·", ("[빈자리] " if "showing_keyword" in t else "[예매 오픈] ") + t["name"])
    print("   오라클에서 서버(인스턴스)를 만들 때 'cloud-init 스크립트' 파일로 넣으면 됩니다.")
    print("   ※ 웹후크 주소가 들어 있으니 이 파일은 다른 사람에게 보내지 마세요.")


def make_phone_script(config):
    if not get_webhook_url():
        print("먼저 5번 메뉴에서 디스코드 웹후크를 설정하세요.")
        return
    try:
        path, size, targets = build_phone_script()
    except RuntimeError as e:
        print(e, "(휴대폰에는 CGV 영화관만 들어갑니다)")
        return
    print(f"\n✅ 휴대폰(Termux) 설치 파일을 만들었습니다: {path}")
    print("   들어간 감시 (CGV만):")
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
        ("등록된 감시 목록", list_targets),
        ("감시 삭제", remove_target),
        ("디스코드 웹후크 설정", set_webhook),
        ("테스트 알림 보내기", test_message),
        ("오라클 서버 설치 파일 만들기 (메가박스)", make_server_script),
        ("휴대폰 설치 파일 만들기 (CGV)", make_phone_script),
        ("빈자리 감시 추가", add_seat_watch),
        ("GitHub에 지금 올리기", manual_push),
    ]
    while True:
        print("\n===== 예매 오픈 알리미 설정 =====")
        for i, (title, _) in enumerate(menu, 1):
            print(f" {i}. {title}")
        print(" 0. 종료")
        s = ask("메뉴 번호 > ")
        if s == "0":
            break
        if s.isdigit() and 1 <= int(s) <= len(menu):
            try:
                menu[int(s) - 1][1](config)
            except Exception as e:
                print("오류가 발생했습니다:", e)


if __name__ == "__main__":
    main()
