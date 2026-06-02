#!/bin/bash
cd "$(dirname "$0")"
echo "Building Krab Ear Agent (Release)..."
swift build -c release
if [ $? -eq 0 ]; then
    echo "Build successful. Launching..."
    ./.build/release/KrabEarAgent
else
    echo "Build failed. Please check the errors above."
    read -p "Press any key to exit..."
fi
