NEW_HOME=/home/$NB_USER
if [ ! -L eos-purdue ]; then ln -sf /eos/purdue/ $NEW_HOME/eos-purdue; fi
if [ -L depot ]; then rm -rf depot; fi
if [ -f depot ]; then rm -rf depot; fi
if [ ! -d depot ]; then mkdir depot; fi
if [ ! -L depot/users ]; then ln -s /depot/cms/users $NEW_HOME/depot/users; fi
if [ ! -L work ]; then ln -sf /work/ $NEW_HOME/work; fi

projects=("hmm" "top" "hh" "sonic")
for project in "${projects[@]}"; do
	if [ ! -L "depot/$project" ]; then
		target="/depot/cms/$project"
		link="$NEW_HOME/depot/$project"
		if [ -e "$link" ]; then
			echo "Skipping $link as it already exists."
		else
			ln -s "$target" "$link"
		fi
	fi
done


