#!/bin/bash

# Determine whether script is running as root
sudo_cmd=""
if [ "$(id -u)" != "0" ]; then
	sudo_cmd="sudo"
	sudo -k
fi

mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key

# Configure Slurm to use maximum available processors and memory
# and start required services
${sudo_cmd} bash <<SCRIPT
useradd -u 616617  -m dkondra
su -l munge -s /usr/sbin/munged &
SCRIPT

export SINGULARITY_CACHEDIR=/depot/cms/purdue-af/interlink/.singularity/cache
export SINGULARITY_TMPDIR=/depot/cms/purdue-af/interlink/.singularity/tmp

# Revoke sudo permissions
if [[ ${sudo_cmd} ]]; then
	sudo -k
fi
