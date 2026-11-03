#!/bin/bash
NEW_HOME=/home/$NB_USER
if [ ! -L "$NEW_HOME/eos-purdue" ]; then ln -sf /eos/purdue/ "$NEW_HOME/eos-purdue"; fi
if [ -L "$NEW_HOME/depot" ] || [ -f "$NEW_HOME/depot" ]; then rm -f "$NEW_HOME/depot"; fi
if [ ! -d "$NEW_HOME/depot" ]; then mkdir "$NEW_HOME/depot"; fi
if [ ! -L "$NEW_HOME/depot/users" ]; then ln -s /depot/cms/users "$NEW_HOME/depot/users"; fi
if [ ! -L "$NEW_HOME/work" ]; then ln -sf /work/ "$NEW_HOME/work"; fi

projects=("hmm" "top" "hh" "sonic")
for project in "${projects[@]}"; do
	target="/depot/cms/$project"
	link="$NEW_HOME/depot/$project"
	if [ -L "$link" ]; then
		continue
	fi
	if [ -e "$link" ]; then
		echo "Skipping $link as it already exists."
	else
		ln -s "$target" "$link"
	fi
done
