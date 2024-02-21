#!/bin/bash

# IMPORTANT NOTE: after using the below section , rotate POSTGRES_PASSWORD, DJANGO_SECRET_KEY 
# echo "DEBUG ENVIRONMENT"
# echo ""
# echo "printenv"
# printenv
# echo ""
# echo "ls"
# ls -ltrah
# echo ""
# echo "pwd"
# pwd
# echo ""
# echo "find libgdal.so"
# find / -name libgdal.so*
# echo ""
# echo "find libgdal.so"
# find / -name libgeos_c.so.1*
# echo ""
# echo "-----------"
#find / -name libproj.so.22
echo "BUILD START"

echo "running df -h"
df -h
echo "---------"
echo "running du -h"
du -h / -d1
echo "---------"

# set location for GDAL dependency files
export LD_LIBRARY_PATH="$(pwd)/.vercel/builders/node_modules/vercel-python-gis/dist/files/"

# Create a virtual environment
echo "Creating a virtual environment..."
python3.9 -m venv venv
source venv/bin/activate

# Install the latest version of pip
echo "Installing the latest version of pip..."
python -m pip install --upgrade pip

# Build the project
echo "Building the project..."
python -m pip install -r requirements.txt

echo "running df -h"
df -h
echo "---------"
echo "running du -h -d2"
du -h / -d2
echo "---------"

# Make migrations
echo "Making migrations..."
python manage.py makemigrations --noinput
python manage.py migrate --noinput

# Collect static  (only in development - for admin module)
# echo "Collecting static files..."
# python manage.py collectstatic --noinput --clear

echo "BUILD END"