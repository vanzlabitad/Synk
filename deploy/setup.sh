#!/usr/bin/env bash
# Synk VPS setup — Ubuntu 22.04 LTS, run as root
# Usage: bash setup.sh
# Requirements: 2 GB RAM minimum (FinBERT stays resident after first load)

set -euo pipefail

echo "=== [1/8] System update ==="
apt-get update && apt-get upgrade -y

echo "=== [2/8] Install Python 3.13 via deadsnakes PPA ==="
apt-get install -y software-properties-common
add-apt-repository ppa:deadsnakes/ppa -y
apt-get update
apt-get install -y python3.13 python3.13-venv
curl -sS https://bootstrap.pypa.io/get-pip.py | python3.13

echo "=== [3/8] Clone repo ==="
cd /root
git clone https://github.com/vanzlabitad/Synk.git synk
cd synk

echo "=== [4/8] Create virtualenv and install dependencies ==="
echo "    (torch is ~2 GB — this takes 5-10 minutes)"
python3.13 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "=== [5/8] Create logs directory ==="
mkdir -p logs

echo ""
echo "=== [6/8] Create .env ==="
echo "    Open nano. Paste your credentials. Save with Ctrl+O → Enter → Ctrl+X."
echo "    Press Enter to open nano..."
read -r _
nano .env
chmod 600 .env

echo "=== [7/8] Basic hardening (firewall + auto security updates) ==="
apt-get install -y ufw unattended-upgrades
ufw allow OpenSSH
ufw --force enable
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "=== [8/8] Install systemd units and start services ==="
cp deploy/synk-bot.service /etc/systemd/system/
cp deploy/synk-watchdog.service /etc/systemd/system/
cp deploy/synk-watchdog.timer /etc/systemd/system/
cp deploy/synk-weekly-review.service /etc/systemd/system/
cp deploy/synk-weekly-review.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable synk-bot.service synk-watchdog.timer synk-weekly-review.timer
systemctl start synk-bot.service synk-watchdog.timer synk-weekly-review.timer

echo ""
echo "=== Done. Verify: ==="
systemctl status synk-bot.service --no-pager
systemctl status synk-watchdog.timer --no-pager
echo ""
echo "Watch bot logs:     tail -f /root/synk/logs/bot.log"
echo "Watch watchdog:     tail -f /root/synk/logs/watchdog.log"
echo "Check heartbeat:    cat /root/synk/logs/heartbeat.json"
