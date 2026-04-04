#!/usr/bin/env bash
# setup_vps.sh — one-shot setup for a fresh Ubuntu 22.04/24.04 VPS
#
# Run as root or with sudo:
#   bash <(curl -sL https://raw.githubusercontent.com/smurray1818/Poly_Up_Down/main/scripts/setup_vps.sh)
#
# Or after cloning:
#   sudo bash scripts/setup_vps.sh

set -euo pipefail

REPO_URL="https://github.com/smurray1818/Poly_Up_Down.git"
INSTALL_DIR="/home/ubuntu/Poly_Up_Down"
SERVICE_USER="ubuntu"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Polymarket Bot — VPS Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. System packages ──────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl

# ── 2. Clone repo ───────────────────────────────────────────────────────────
echo "[2/6] Cloning repository..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "      Repo already exists — pulling latest..."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull
else
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── 3. Python venv + dependencies ───────────────────────────────────────────
echo "[3/6] Creating venv and installing dependencies..."
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -q \
    --only-binary :all: \
    -r "$INSTALL_DIR/requirements.txt"

# ── 4. .env setup ───────────────────────────────────────────────────────────
echo "[4/6] Configuring .env..."
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  .env created from .env.example                     │"
    echo "  │  Edit it now before the bot starts:                 │"
    echo "  │                                                     │"
    echo "  │    nano $INSTALL_DIR/.env        │"
    echo "  │                                                     │"
    echo "  │  Required values:                                   │"
    echo "  │    GITHUB_TOKEN  — for CSV sync pushes              │"
    echo "  │    GITHUB_REPO   — smurray1818/Poly_Up_Down         │"
    echo "  │    GITHUB_LABEL  — latency-tracking                 │"
    echo "  │    BTC_TARGET_PRICE — current BTC price             │"
    echo "  │    ETH_TARGET_PRICE — current ETH price             │"
    echo "  │                                                     │"
    echo "  │  CLOB_API_KEY/SECRET/PASSPHRASE can stay as        │"
    echo "  │  placeholders while DRY_RUN=true + PAPER_TRADING=true│"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""
    read -rp "  Press Enter after editing .env to continue..."
else
    echo "      .env already exists — skipping."
fi

# ── 5. Configure git for the sync script ────────────────────────────────────
echo "[5/6] Configuring git credentials for CSV sync..."
GITHUB_TOKEN=$(grep GITHUB_TOKEN "$INSTALL_DIR/.env" | cut -d= -f2 | tr -d '[:space:]')
if [[ -n "$GITHUB_TOKEN" && "$GITHUB_TOKEN" != "ghp_your_token_here" ]]; then
    # Store token in git credential store so sync_trades.sh can push
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" config credential.helper store
    # Write credentials file
    echo "https://smurray1818:${GITHUB_TOKEN}@github.com" \
        | sudo -u "$SERVICE_USER" tee /home/ubuntu/.git-credentials > /dev/null
    chmod 600 /home/ubuntu/.git-credentials
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" config user.email "bot@polymarket-bot"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" config user.name  "Polymarket Bot"
    echo "      Git credentials configured."
else
    echo "      WARNING: GITHUB_TOKEN not set — CSV sync pushes will fail."
fi

# ── 6. Install and start systemd services ───────────────────────────────────
echo "[6/6] Installing systemd services..."
SCRIPTS="$INSTALL_DIR/scripts"

# Swap placeholder user if not ubuntu
sed "s|User=ubuntu|User=$SERVICE_USER|g; s|/home/ubuntu|/home/$SERVICE_USER|g" \
    "$SCRIPTS/polymarket-bot.service"    > /etc/systemd/system/polymarket-bot.service
sed "s|User=ubuntu|User=$SERVICE_USER|g; s|/home/ubuntu|/home/$SERVICE_USER|g" \
    "$SCRIPTS/polymarket-sync.service"   > /etc/systemd/system/polymarket-sync.service
cp "$SCRIPTS/polymarket-sync.timer"     /etc/systemd/system/polymarket-sync.timer

systemctl daemon-reload
systemctl enable --now polymarket-bot
systemctl enable --now polymarket-sync.timer

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Done! Bot is running."
echo ""
echo " Useful commands:"
echo "   journalctl -u polymarket-bot -f        # live bot logs"
echo "   journalctl -u polymarket-sync -f        # sync logs"
echo "   systemctl status polymarket-bot         # status"
echo "   systemctl restart polymarket-bot        # restart after .env changes"
echo "   systemctl list-timers polymarket-sync   # next sync time"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
