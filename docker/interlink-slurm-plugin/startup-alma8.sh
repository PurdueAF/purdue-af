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
sed -i "s/<<HOSTNAME>>/$(hostname)/" /etc/slurm/slurm.conf
sed -i "s/<<CPU>>/$(nproc)/" /etc/slurm/slurm.conf
sed -i "s/<<MEMORY>>/$(if [[ \"$(slurmd -C)\" =~ RealMemory=([0-9]+) ]]; then echo \"${BASH_REMATCH[1]}\"; else exit 100; fi)/" /etc/slurm/slurm.conf
# Start munged as the munge user (Alma8 container best practice)
su -l munge -s /usr/sbin/munged &
# service slurmd start
# service slurmctld start
SCRIPT

# Revoke sudo permissions
if [[ ${sudo_cmd} ]]; then
	sudo -k
fi
