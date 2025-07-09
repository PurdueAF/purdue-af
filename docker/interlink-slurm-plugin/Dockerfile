FROM golang:1.22 as build-stage

WORKDIR /app

# COPY . .
RUN git clone https://github.com/interlink-hq/interlink-slurm-plugin.git /app
RUN git checkout main
RUN CGO_ENABLED=0 GOOS=linux go build -o bin/slurm-sidecar cmd/main.go


# Deploy the application binary into a lean image
#FROM ubuntu:latest AS build-release-stage
FROM ubuntu:25.04

# Settings for all images
ENV TIMEZONE=America/New_York

# Run system updates
RUN apt-get update && apt-get -y upgrade

# Set time zone
RUN ln -sn /usr/share/zoneinfo/${TIMEZONE} /etc/localtime \
    && echo ${TIMEZONE} > /etc/timezone \
    && apt-get -y install tzdata

# Set locale
RUN apt-get -y install locales \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 LC_MESSAGES=POSIX

# Install system packages
RUN DEBIAN_FRONTEND=noninteractive \
    apt-get -y install munge \
                   slurm-wlm=24.05.2-1

# Slurm configuration
COPY slurm.conf /etc/slurm/slurm.conf
RUN mkdir -p /var/spool/slurmctld \
    && chown slurm:slurm /var/spool/slurmctld

# Startup configuration
COPY startup.sh /etc/startup.sh
RUN chmod 555 /etc/startup.sh

WORKDIR /root

COPY --from=build-stage /app/bin/slurm-sidecar /sidecar/slurm-sidecar

ENV SLURMCONFIGPATH=/root/SlurmConfig.yaml

RUN mkdir -p /cvmfs/grid.cern.ch/etc/grid-security

CMD ["/bin/sh", "-c", "/etc/startup.sh && SHARED_FS=true /sidecar/slurm-sidecar"]