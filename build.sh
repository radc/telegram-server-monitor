set -eo pipefail

echo "Deleting/Creating Service Folder"
sudo rm -rf /opt/telegram-server-monitor
sudo mkdir -p /opt/telegram-server-monitor

echo "Copying Files"
sudo cp * /opt/telegram-server-monitor/
sudo cp .env /opt/telegram-server-monitor/.env

sudo chown $USER:$USER /opt/telegram-server-monitor
cd /opt/telegram-server-monitor

sudo chmod 777 aow_subscribers.txt

echo "Creating Python Environment"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Creating Service"
sudo cp server-monitor-bot.service.example /etc/systemd/system/server-monitor-bot.service

echo "Reloading systemd"
sudo systemctl daemon-reload

echo "Enabling boot"
sudo systemctl enable server-monitor-bot.service

echo "Starting service"
sudo systemctl start server-monitor-bot.service