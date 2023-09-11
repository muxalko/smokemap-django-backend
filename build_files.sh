#!/bin/bash
echo "DEBUG ENVIRONMENT"
echo ""
echo "printenv"
printenv
echo ""
echo "ls"
ls -ltrah
echo ""
echo "pwd"
pwd
echo ""
echo "find libgdal.so"
find / -name libgdal.so*
echo ""
echo "find libgdal.so"
find / -name libgeos_c.so.1*
echo ""
echo "-----------"
echo "BUILD START"

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

# Make migrations
echo "Making migrations..."
python manage.py makemigrations --noinput
python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear


echo "BUILD END"