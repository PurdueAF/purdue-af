NEW_HOME=/home/$NB_USER
if [ ! -d eos-purdue ]; then ln -s /eos/purdue/ $NEW_HOME/eos-purdue; fi;
if [ -L depot ]; then rm -rf depot; fi;
if [ -f depot ]; then rm -rf depot; fi;
if [ ! -d depot ]; then mkdir depot; fi;
if [ ! -L depot/users ]; then ln -s /depot/cms/users $NEW_HOME/depot/users; fi;

projects=("hmm" "top" "hh")
for project in "${projects[@]}"; do
    if [ ! -L "depot/$project" ]; then
        ln -s "/depot/cms/$project" "$NEW_HOME/depot/$project"
    fi
done