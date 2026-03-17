#!/bin/bash
# Lockwayland supervisor shell script

while true; do
    # Run the locker
    export LD_PRELOAD=/usr/lib64/libgtk4-layer-shell.so.1.3.0
    python3 locker.py
    
    # Check the exit code
    EXIT_CODE=$?
    
    # If it exited with 0, it means the user authenticated successfully.
    if [ $EXIT_CODE -eq 0 ]; then
        exit 0
    fi
    
    # If it crashed (non-zero), wait a fraction of a second and loop (re-lock)
    sleep 0.01
done
