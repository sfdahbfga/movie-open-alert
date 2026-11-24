# GitHub 저장소 자동 동기화
# 5분마다 저장소의 최신 커밋 번호만 확인 (요청 1번, 수십 바이트)
# 바뀌었으면 코드와 config.json을 그 커밋 기준으로 받아서 검사 → 통과하면 적용 후 다시 시작
# (디스코드 웹후크 주소는 저장소에 올리지 않고 각 기기의 secret.json에만 있음)
import os
import sys
import json
import time
import hashlib

from common import BASE_DIR, http_request, load_json, save_json, save_log_info, save_log_error

DEVICE_PATH = os.path.join(BASE_DIR, "device.json")   # 이 기기 설정 (어떤 체인을 돌릴지, 저장소 주소)
CODE_FILES = ["main.py", "common.py", "cgv.py", "megabox.py", "lotte.py", "seats.py", "sync.py", "coupons.py"]
CHECK_SECONDS = 300  # 5분 (GitHub 비로그인 요청 제한: 시간당 60번 → 5분마다면 12번이라 여유)


def load_device():
    return load_json(DEVICE_PATH, {})


def device_role(device=None):
    """이 기기의 역할: 'server'(오라클) / 'phone'(휴대폰). 예전 설치본은 chains로 추측"""
    device = load_device() if device is None else device
    if device.get("role"):
        return device["role"]
    chains = set(device.get("chains") or [])
    if "megabox" in chains:
        return "server"
    if "cgv" in chains:
        return "phone"
    return ""


def device_chains(config, device=None):
    """이 기기가 맡을 극장 체인. 저장소 설정(device_roles)을 먼저 따르고, 없으면 설치 때 정한 값.
    빈 집합이면 '전부' (PC처럼 기기 설정이 없는 곳)"""
    device = load_device() if device is None else device
    role = device_role(device)
    roles = config.get("device_roles") or {}
    if role and role in roles:
        return set(roles[role])
    return set(device.get("chains") or [])


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def latest_commit(repo):
    # 'application/vnd.github.sha' 형식으로 받으면 커밋 번호 문자열만 옴
    body = http_request(f"https://api.github.com/repos/{repo}/commits/{device_branch()}",
                        headers={"Accept": "application/vnd.github.sha"}, timeout=15)
    sha = body.decode("utf-8").strip()
    if len(sha) != 40:
        raise RuntimeError(f"커밋 번호 형식이 이상함: {sha[:60]}")
    return sha


def device_branch():
    return load_device().get("branch", "main")


def download(repo, sha, name):
    return http_request(f"https://raw.githubusercontent.com/{repo}/{sha}/{name}", timeout=20)


def fetch_update(repo, sha):
    """그 커밋의 파일들을 받아서 검사. 바뀐 파일만 {이름: 내용}으로 돌려줌"""
    changed = {}
    for name in CODE_FILES + ["config.json"]:
        try:
            data = download(repo, sha, name)
        except Exception as e:
            if name == "config.json":
                raise
            save_log_info(f"[동기화] {name}을 받지 못해 건너뜀: {e}", True)
            continue
        path = os.path.join(BASE_DIR, name)
        old = open(path, "rb").read() if os.path.exists(path) else b""
        if _sha256(old) == _sha256(data):
            continue
        # 검사: 파이썬 문법 / JSON 형식이 틀리면 적용하지 않음
        if name.endswith(".py"):
            compile(data.decode("utf-8"), name, "exec")
        else:
            json.loads(data.decode("utf-8-sig"))
        changed[name] = data
    return changed


def apply_update(changed):
    for name, data in changed.items():
        path = os.path.join(BASE_DIR, name)
        tmp = path + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # 한 번에 바꿔치기 (쓰다 만 파일이 남지 않게)


def describe_config_change(old_cfg, new_cfg, chains):
    """감시 목록이 어떻게 바뀌었는지 사람이 읽을 수 있게"""
    def names(cfg, key):
        return {e.get("name", "?") for e in cfg.get(key) or [] if not chains or e.get("chain") in chains}
    lines = []
    for key, label in (("targets", "예매 오픈"), ("seat_watches", "빈자리"), ("coupon_watches", "쿠폰")):
        before, after = names(old_cfg, key), names(new_cfg, key)
        lines += [f"➕ {label}: {n}" for n in sorted(after - before)]
        lines += [f"➖ {label}: {n}" for n in sorted(before - after)]
    return lines


def restart():
    save_log_info("[동기화] 새 버전으로 다시 시작합니다.", True)
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, "-u", os.path.join(BASE_DIR, "main.py")])


def sync_main(message_queue):
    device = load_device()
    repo = device.get("repo")
    if not repo:
        return  # PC 등 동기화 설정이 없는 곳에서는 아무것도 안 함
    first_wait = 60  # 켜지고 1분 뒤 첫 확인
    time.sleep(first_wait)
    fail = 0
    while True:
        try:
            try:
                sha = latest_commit(repo)
            except Exception as e:
                # 커밋 번호 확인이 안 되면 브랜치 이름으로 파일을 직접 받아 비교 (조금 더 무거움)
                save_log_info(f"[동기화] 커밋 번호 확인 실패, 파일 직접 비교로 대신함: {e}")
                sha = device_branch()
                device["applied_sha"] = None
            # 새로 추가된 코드 파일이 이 기기에 아직 없으면(예: 예전 버전이 모르던 lotte.py) 강제로 받음
            missing = [n for n in CODE_FILES if not os.path.exists(os.path.join(BASE_DIR, n))]
            if sha != device.get("applied_sha") or missing:
                old_cfg = load_json(os.path.join(BASE_DIR, "config.json"), {})
                changed = fetch_update(repo, sha)
                if len(sha) == 40:
                    device["applied_sha"] = sha
                    save_json(DEVICE_PATH, device)
                if changed:
                    new_cfg = (json.loads(changed["config.json"].decode("utf-8-sig"))
                               if "config.json" in changed else old_cfg)
                    lines = describe_config_change(old_cfg, new_cfg, device_chains(new_cfg, device))
                    code = [n for n in changed if n.endswith(".py")]
                    apply_update(changed)
                    msg = ["🔄 저장소에서 변경 사항을 받아 다시 시작합니다."]
                    if code:
                        msg.append("· 프로그램 업데이트: " + ", ".join(code))
                    msg += lines or (["· 이 기기의 감시 목록 변화 없음"] if "config.json" in changed else [])
                    message_queue.put("\n".join(msg))
                    time.sleep(8)  # 디스코드 알림이 나갈 시간
                    restart()
            fail = 0
        except Exception as e:
            fail += 1
            save_log_error(f"[동기화] 확인 실패 ({fail}회 연속): {e}")
            if fail == 12:  # 약 1시간
                message_queue.put(f"⚠️ 저장소 자동 동기화가 1시간째 실패하고 있어요. 감시는 계속 돌아갑니다.\n원인: {e}")
        time.sleep(CHECK_SECONDS)
