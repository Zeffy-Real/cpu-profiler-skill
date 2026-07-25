#!/bin/bash
# install.sh - One-click installation for CPU Profiler
# Usage: sudo bash install.sh

set -e

echo "=== CPU Profiler Installation ==="

# 1. Check dependencies
echo "[1/5] Checking dependencies..."

for cmd in perf python3; do
    if ! command -v $cmd &> /dev/null; then
        echo "ERROR: $cmd is required but not installed."
        exit 1
    fi
done

# Check FlameGraph tools
for tool in flamegraph.pl stackcollapse-perf.pl; do
    if ! command -v $tool &> /dev/null; then
        echo "WARNING: $tool not found. Installing FlameGraph tools..."
        git clone https://github.com/brendangregg/FlameGraph.git /tmp/FlameGraph 2>/dev/null || true
        cp /tmp/FlameGraph/flamegraph.pl /tmp/FlameGraph/stackcollapse-perf.pl /usr/local/bin/
        chmod +x /usr/local/bin/flamegraph.pl /usr/local/bin/stackcollapse-perf.pl
    fi
done

# 2. Install Python dependencies
echo "[2/5] Installing Python dependencies..."
pip3 install -r requirements.txt

# 3. Create data directory
echo "[3/5] Creating data directory..."
mkdir -p /var/lib/cpu-profiler

# 4. Install to /opt
echo "[4/5] Installing to /opt/cpu-profiler..."
mkdir -p /opt/cpu-profiler
cp -r src/ /opt/cpu-profiler/src/
cp -r systemd/ /opt/cpu-profiler/systemd/
cp requirements.txt /opt/cpu-profiler/

# 5. Register systemd services
echo "[5/5] Registering systemd services..."
cp systemd/cpu-profiler-collector.service /etc/systemd/system/
cp systemd/cpu-profiler-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable cpu-profiler-collector cpu-profiler-api

# 6. Set perf permissions
echo "Setting perf permissions..."
echo -1 > /proc/sys/kernel/perf_event_paranoid 2>/dev/null || true
grep -q perf_event_paranoid /etc/sysctl.conf 2>/dev/null || \
    echo 'kernel.perf_event_paranoid = -1' >> /etc/sysctl.conf

echo ""
echo "=== Installation Complete ==="
echo "Start services with:"
echo "  sudo systemctl start cpu-profiler-collector cpu-profiler-api"
echo "Check status with:"
echo "  sudo systemctl status cpu-profiler-collector cpu-profiler-api"
echo "API available at: http://localhost:8765/api/v1/health"
