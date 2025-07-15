#!/bin/bash

# Exit immediately on error
set -e

find . | grep -E "(__pycache__|\.pyc|\.pyo$)" | xargs rm -rf

rm -rf vanna_agent.zip
zip -r vanna_agent.zip app Dockerfile public requirements.txt server.py