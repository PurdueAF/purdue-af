#!/bin/bash

# Determine whether script is running as root
sudo_cmd=""
if [ "$(id -u)" != "0" ]; then
	sudo_cmd="sudo"
	sudo -k
fi

# Configure permissions
chmod 0755 /run
mkdir -p /run/munge
chown munge:munge /run/munge
chmod 0711 /run/munge
mkdir -p /run/lock/
touch /run/lock/slurm

# Configure Slurm to use maximum available processors and memory
# and start required services
${sudo_cmd} bash <<SCRIPT
sed -i "s/<<HOSTNAME>>/$(hostname)/" /etc/slurm/slurm.conf
sed -i "s/<<CPU>>/$(nproc)/" /etc/slurm/slurm.conf
sed -i "s/<<MEMORY>>/$(if [[ "$(slurmd -C)" =~ RealMemory=([0-9]+) ]]; then echo "${BASH_REMATCH[1]}"; else exit 100; fi)/" /etc/slurm/slurm.conf
service munge start
service slurmd start
service slurmctld start
SCRIPT

# Revoke sudo permissions
if [[ ${sudo_cmd} ]]; then
	sudo -k
fi
