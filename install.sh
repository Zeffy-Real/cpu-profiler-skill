#!/bin/bash
set -e

echo "=== CPU Profiler Skill - Installation ==="

# Check dependencies
echo "Checking dependencies..."
command -v perf >/dev/null 2>&1 || { echo "ERROR: perf not found"; exit 1; }
command -v flamegraph.pl >/dev/null 2>&1 || command -v flamegraph >/dev/null 2>&1 || { echo "ERROR: flamegraph not found"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found"; exit 1; }

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install -r requirements.txt

# Create data directory
echo "Creating data directory..."
sudo mkdir -p /var/lib/cpu-profiler
sudo chown $(whoami) /var/lib/cpu-profiler

# Install systemd services
echo "Installing systemd services..."
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cpu-profiler-collector cpu-profiler-api

echo ""
echo "=== Installation complete! ==="
echo "Start services with: sudo systemctl start cpu-profiler-collector cpu-profiler-api"
