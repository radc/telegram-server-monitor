set -eo pipefail

echo "Creating Build Folder"
mkdir -p build
cp -r ./* build/
cd build

echo "Creating Python Environment"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Running Bot"
python bot.py

