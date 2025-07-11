#!/bin/bash

# Script to purge README cache by extracting image URLs and purging their cache
# Based on the script provided by the user

set -e  # Exit on any error

CURL=curl
GREP=grep

README_TMP=readme.html
# Updated for PurdueAF/purdue-af repository
USER=PurdueAF
REPO=purdue-af

echo "Starting README cache purge for $USER/$REPO..."

# Download the README.md from GitHub
echo "Downloading README.md from GitHub..."
$CURL -s "https://github.com/$USER/$REPO/blob/main/README.md" > "$README_TMP"

# Extract image URLs that use GitHub's camo cache and purge them
echo "Extracting and purging cached image URLs..."
$GREP -Eo '<img src=\\"[^"]+\\"' "$README_TMP" | \
$GREP camo | \
$GREP -Eo 'https[^"\\]+' | \
while read -r url; do
    echo "Purging cache for: $url"
    $CURL -w "\n" -s -X PURGE "$url"
done

# Clean up temporary file
rm -f "$README_TMP"

echo "README cache purge completed!" 