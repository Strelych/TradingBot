#!/bin/bash
set -e

cd /workspace

# Copy updated files to server using sshpass
SSHPASS="your_password_here" sshpass -e scp -o StrictHostKeyChecking=no analyzer.py adapter.py server.py root@185.78.76.248:/root/bybit_scalper/

# Restart service on remote server
sshpass -e ssh -o StrictHostKeyChecking=no root@185.78.76.248 "systemctl restart bybit-scalper && sleep 3"

echo "Deploy complete!"
