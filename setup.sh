#!/bin/bash
set -e

echo "=== nx-bench setup ==="

# Check prerequisites
command -v python3 >/dev/null 2>&1 || { echo "Error: python3 is required"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "Error: docker is required"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Error: git is required"; exit 1; }

# Check for .env file
if [ ! -f .env ]; then
    echo "Error: .env file not found. Create one with:"
    echo "  OPENAI_API_KEY=sk-..."
    echo "  GITHUB_TOKEN=ghp_..."
    exit 1
fi

# Install Python dependencies
echo "[1/4] Installing Python dependencies..."
pip install -r requirements.txt

# Clone NetworkX
if [ ! -d "networkx" ]; then
    echo "[2/4] Cloning networkx/networkx..."
    git clone https://github.com/networkx/networkx.git
else
    echo "[2/4] networkx/ already exists, skipping clone"
fi

# Clone and install mini-swe-agent
if [ ! -d "mini-swe-agent" ]; then
    echo "[3/4] Cloning and installing mini-swe-agent..."
    git clone https://github.com/SWE-agent/mini-swe-agent.git
    pip install -e mini-swe-agent/
else
    echo "[3/4] mini-swe-agent/ already exists, skipping clone"
    pip install -e mini-swe-agent/ 2>/dev/null || true
fi

# Build Docker scoring image
echo "[4/4] Building Docker scoring image..."
docker build -f Dockerfile.eval -t nx-eval .

echo ""
echo "=== Setup complete ==="
echo "To run the benchmark:"
echo "  python run_benchmark.py --output results/"
echo "To analyze results:"
echo "  python analyze.py results/"
