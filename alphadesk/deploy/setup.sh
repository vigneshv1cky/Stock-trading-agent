#!/usr/bin/env bash
# AlphaDesk VM setup — Ubuntu 24.04 aarch64 (Oracle Always Free A1).
# Run from the repo root after cloning:  bash alphadesk/deploy/setup.sh
set -euo pipefail

echo "── system packages ──"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

echo "── swap (small-host guard: LLM CLI spawns need headroom) ──"
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
if [[ "$TOTAL_MB" -lt 2048 && ! -f /swapfile ]]; then
  echo "  ${TOTAL_MB}MB RAM detected — creating 3GB swapfile"
  sudo fallocate -l 3G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  sudo sysctl -q vm.swappiness=10
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-alphadesk.conf >/dev/null
else
  echo "  ${TOTAL_MB}MB RAM — no swap needed (or swapfile exists)"
fi

echo "── python venv + deps ──"
python3 --version
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r alphadesk/deploy/requirements.txt

echo "── smoke check: bundled Claude CLI works on this arch ──"
./.venv/bin/python - <<'PY'
import os, pathlib, subprocess
import claude_agent_sdk
p = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
assert p.is_file() and os.access(p, os.X_OK), f"bundled CLI missing: {p}"
out = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=30)
assert out.returncode == 0, out.stderr[:200]
print("  bundled CLI OK:", out.stdout.strip())
PY

echo "── .env ──"
if [[ ! -f .env ]]; then
  cp alphadesk/deploy/env.example .env
  chmod 600 .env
  echo "  created .env from template — EDIT IT NOW: nano .env"
else
  echo "  .env exists — leaving it alone"
fi

echo "── systemd service ──"
sudo cp alphadesk/deploy/alphadesk.service /etc/systemd/system/alphadesk.service
sudo systemctl daemon-reload
sudo systemctl enable alphadesk

echo "── open port 8000 in the VM's local firewall (Oracle Ubuntu gotcha) ──"
if sudo iptables -C INPUT -m state --state NEW -p tcp --dport 8000 -j ACCEPT 2>/dev/null; then
  echo "  rule already present"
else
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
  sudo apt-get install -y -qq iptables-persistent >/dev/null 2>&1 || true
  sudo netfilter-persistent save 2>/dev/null || echo "  (netfilter-persistent not available — rule is live but not persisted)"
fi

echo ""
echo "READY. Next steps:"
echo "  1. nano .env                       # paste all secrets"
echo "  2. sudo systemctl start alphadesk"
echo "  3. journalctl -u alphadesk -f      # watch it come up"
echo "  4. http://<VM_PUBLIC_IP>:8000      # dashboard (Basic Auth)"
echo "  5. optional first-day warmup:"
echo "     PYTHONPATH=. ./.venv/bin/python -m alphadesk.main backfill --hours 72"
