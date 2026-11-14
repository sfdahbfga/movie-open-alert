# 오라클 클라우드 서버용 설치 스크립트(cloud-init) 만들기
# 만들어진 oracle_setup.sh를 서버 생성 화면의 'cloud-init 스크립트' 칸에 넣으면,
# 서버가 처음 켜질 때 알리미를 설치하고 24시간 자동 실행(꺼져도 자동 재시작)되도록 설정합니다.
import io
import os
import base64
import tarfile
from common import BASE_DIR, load_config, save_json, get_webhook_url

OUTPUT_PATH = os.path.join(BASE_DIR, "oracle_setup.sh")
APP_FILES = ["main.py", "common.py", "cgv.py", "megabox.py", "seats.py", "sync.py"]
LIMIT = 30000  # 오라클 cloud-init 크기 제한(약 32KB)보다 조금 작게

TEMPLATE = r"""#!/bin/bash
# movie-open-alert installer (runs once when the server boots for the first time)
# NOTE: keep this script ASCII-only. The Oracle console rejects non-ASCII cloud-init scripts.
# Install log: /var/log/movie-open-alert-install.log
exec > /var/log/movie-open-alert-install.log 2>&1
set -x
APP=/opt/movie-open-alert
mkdir -p "$APP"

# 1) Unpack program files (theater list + Discord webhook included)
base64 -d > /tmp/movie-open-alert.tar.gz <<'__APP_FILES__'
{payload}
__APP_FILES__
tar -xzf /tmp/movie-open-alert.tar.gz -C "$APP"
rm -f /tmp/movie-open-alert.tar.gz

# 2) Korean time zone
timedatectl set-timezone Asia/Seoul || true

# 3) Make sure python3 exists (Ubuntu has it by default)
if ! command -v python3 >/dev/null; then
  (apt-get update && apt-get install -y python3) || dnf install -y python3 || yum install -y python3
fi
PY=$(command -v python3)

# 4) Dedicated service account
id alert >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin alert
chown -R alert:alert "$APP"
chmod 600 "$APP/secret.json"

# 5) Run 24/7, restart automatically on crash or reboot
cat > /etc/systemd/system/movie-open-alert.service <<__SERVICE__
[Unit]
Description=CGV/Megabox booking open alert
Wants=network-online.target
After=network-online.target

[Service]
User=alert
WorkingDirectory=$APP
Environment=TZ=Asia/Seoul
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=$PY -u main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
__SERVICE__
systemctl daemon-reload
systemctl enable --now movie-open-alert.service
echo "install done"
"""


PHONE_TEMPLATE = r"""#!/data/data/com.termux/files/usr/bin/bash
# movie-open-alert installer for Android (Termux). ASCII only.
set -e
APP="$HOME/movie-open-alert"
echo "[1/4] installing python..."
pkg install -y python >/dev/null
echo "[2/4] unpacking program..."
pkill -f "movie-open-alert/run.sh" 2>/dev/null || true
pkill -f "python -u main.py" 2>/dev/null || true
mkdir -p "$APP"
base64 -d > "$PREFIX/tmp/movie-open-alert.tar.gz" <<'__APP_FILES__'
{payload}
__APP_FILES__
tar -xzf "$PREFIX/tmp/movie-open-alert.tar.gz" -C "$APP"
rm -f "$PREFIX/tmp/movie-open-alert.tar.gz"
chmod 600 "$APP/secret.json"
echo "[3/4] creating start scripts..."
cat > "$APP/run.sh" <<'__RUN__'
#!/data/data/com.termux/files/usr/bin/bash
# keep the alert running; restart after 30s if it stops
cd "$HOME/movie-open-alert"
termux-wake-lock 2>/dev/null || true
while true; do
  TZ=Asia/Seoul python -u main.py >> alert.out 2>&1
  sleep 30
done
__RUN__
chmod +x "$APP/run.sh"
# auto start when the phone reboots (needs the Termux:Boot app, optional)
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-movie-open-alert" <<'__BOOT__'
#!/data/data/com.termux/files/usr/bin/bash
nohup "$HOME/movie-open-alert/run.sh" >/dev/null 2>&1 &
__BOOT__
chmod +x "$HOME/.termux/boot/start-movie-open-alert"
# shortcut command: type 'alert' to (re)start
grep -q "movie-open-alert/run.sh" "$HOME/.bashrc" 2>/dev/null || \
  echo 'alias alert="pkill -f movie-open-alert/run.sh; pkill -f \"python -u main.py\"; nohup ~/movie-open-alert/run.sh >/dev/null 2>&1 & echo started"' >> "$HOME/.bashrc"
echo "[4/4] starting..."
nohup "$APP/run.sh" >/dev/null 2>&1 &
sleep 2
echo ""
echo "DONE. Check Discord for the start message."
echo "Keep Termux running in the background (do not tap 'Exit' in its notification)."
"""


PHONE_OUTPUT_PATH = os.path.join(BASE_DIR, "phone_setup.sh")


def _build(template, output_path, chains):
    """chains에 해당하는 영화관만 담아서 설치 스크립트를 만듦"""
    config = load_config()
    mine = [e for e in config["targets"] + (config.get("seat_watches") or []) if e["chain"] in chains]
    if not mine and not config.get("sync_repo"):
        raise RuntimeError("이 설치 파일에 넣을 감시 대상이 없습니다.")
    # 이 기기가 맡을 체인 + 자동 동기화 저장소 (감시 목록 전체는 그대로 넣고, 실행할 때 체인으로 거름)
    device = {"chains": sorted(chains), "repo": config.get("sync_repo", ""), "branch": "main"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in APP_FILES:
            tar.add(os.path.join(BASE_DIR, name), arcname=name)
        for name, data in (("config.json", config), ("device.json", device),
                           ("secret.json", {"discord_webhook_url": get_webhook_url()})):
            tmp = os.path.join(BASE_DIR, f".tmp_{name}")
            save_json(tmp, data)
            tar.add(tmp, arcname=name)
            os.remove(tmp)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    # 한 줄이 너무 길지 않게 76자씩 줄바꿈
    payload = "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
    script = template.replace("{payload}", payload)
    if not script.isascii():
        raise RuntimeError("설치 스크립트에 영어가 아닌 글자가 섞여 있습니다")
    with open(output_path, "w", encoding="ascii", newline="\n") as f:
        f.write(script)
    size = len(script.encode("ascii"))
    if output_path == OUTPUT_PATH and size > LIMIT:
        print(f"⚠ 스크립트가 {size:,}바이트로 오라클 제한(약 32KB)에 가깝거나 넘습니다. 영화관 수를 줄여 보세요.")
    return output_path, size, mine


# CGV는 클라우드(해외/데이터센터) 서버 접속을 막아서(403) 오라클에는 메가박스만 넣음
SERVER_CHAINS = {"megabox"}
PHONE_CHAINS = {"cgv"}


def build_cloud_init(output_path=OUTPUT_PATH):
    return _build(TEMPLATE, output_path, SERVER_CHAINS)


def build_phone_script(output_path=PHONE_OUTPUT_PATH):
    return _build(PHONE_TEMPLATE, output_path, PHONE_CHAINS)


if __name__ == "__main__":
    print(build_cloud_init())
