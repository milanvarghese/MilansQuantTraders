#!/bin/bash
# Deploy Polymarket Weather Bot on a Linux VM (Ubuntu/Debian)
# Usage: bash deploy.sh

set -e

echo "=== Polymarket Weather Bot - VM Setup ==="

# Install Python 3.11+ if needed
if ! command -v python3 &> /dev/null; then
    echo "Installing Python..."
    sudo apt update && sudo apt install -y python3 python3-pip python3-venv
fi

# Create project directory
PROJECT_DIR="$HOME/polymarket-bot"
mkdir -p "$PROJECT_DIR/logs"

# Copy files (assumes you've SCP'd or cloned the repo)
cd "$PROJECT_DIR"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo ">>> IMPORTANT: Edit .env with your wallet private key <<<"
    echo ">>> Run: nano $PROJECT_DIR/.env <<<"
    echo ""
fi

# Create systemd service
sudo tee /etc/systemd/system/polymarket-bot.service > /dev/null <<EOF
[Unit]
Description=Polymarket Weather Trading Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR/scripts
ExecStart=$PROJECT_DIR/venv/bin/python weather_scanner.py
Restart=on-failure
RestartSec=30
Environment=PYTHONPATH=$PROJECT_DIR/scripts

[Install]
WantedBy=multi-user.target
EOF

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:     nano $PROJECT_DIR/.env"
echo "  2. Paper trade:   cd $PROJECT_DIR/scripts && ../venv/bin/python weather_scanner.py --scan-once"
echo "  3. Run loop:      cd $PROJECT_DIR/scripts && ../venv/bin/python weather_scanner.py"
echo "  4. Go live:       cd $PROJECT_DIR/scripts && ../venv/bin/python weather_scanner.py --live"
echo "  5. As service:    sudo systemctl enable --now polymarket-bot"
echo "  6. View logs:     journalctl -u polymarket-bot -f"
echo ""
echo "To derive API creds (after setting PRIVATE_KEY):"
echo "  cd $PROJECT_DIR && venv/bin/python setup_creds.py"
