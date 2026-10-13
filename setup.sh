#!/usr/bin/env bash
# One-time setup on the Orange Pi 5: build xmrig, check ccminer, reserve
# huge pages, and generate a systemd unit. Run from the project folder.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
DIR=$PWD

if [ "$(uname -m)" != "aarch64" ]; then
  echo "This script is meant to run on the Orange Pi 5 (aarch64), not $(uname -m)." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y git python3 build-essential cmake libuv1-dev libssl-dev libhwloc-dev \
  libcurl4-openssl-dev libjansson-dev libomp-dev

# The two upstream projects are not vendored in this repo; fetch them if missing.
[ -d xmrig ] || git clone --depth 1 --branch v6.26.0 https://github.com/xmrig/xmrig.git
[ -d Android-Mining ] || git clone --depth 1 https://github.com/Oink70/Android-Mining.git

echo "== building xmrig =="
cmake -S xmrig -B xmrig/build -DCMAKE_BUILD_TYPE=Release -DWITH_OPENCL=OFF -DWITH_CUDA=OFF
cmake --build xmrig/build -j"$(nproc)"

echo "== checking ccminer =="
chmod +x Android-Mining/ccminer
if Android-Mining/ccminer --version >/dev/null 2>&1; then
  echo "ccminer runs."
else
  echo "!! Android-Mining/ccminer does not start on this system. Missing libraries:"
  ldd Android-Mining/ccminer | grep "not found" || true
  echo "   Either install the missing library (commonly libssl1.1) or build ccminer"
  echo "   from source and point verus.binary in config.json at the result."
fi

# RandomX wants ~2.5 GB of 2 MB huge pages; only reserve them if RAM allows.
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "$mem_kb" -ge 7000000 ]; then
  echo "vm.nr_hugepages=1280" | sudo tee /etc/sysctl.d/90-dualminer-hugepages.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/90-dualminer-hugepages.conf
  echo "Reserved 1280 huge pages for RandomX."
else
  echo "Less than 8 GB RAM: skipping huge page reservation (Monero will run slower)."
fi

[ -f config.json ] || { cp config.example.json config.json; chmod 600 config.json; }

cat > dualminer.service <<EOF
[Unit]
Description=Monero + Verus dual CPU miner
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/dualminer.py run
Restart=on-failure
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF

Done. Next:
  1. Edit $DIR/config.json and set both wallet addresses.
  2. Try it:           ./dualminer.py
  3. Run at boot:      sudo cp dualminer.service /etc/systemd/system/ && sudo systemctl enable --now dualminer
EOF
