FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    cmake \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN python -m pip install --upgrade pip

# Clone A2RCHI repository
RUN git clone https://github.com/kondratyevd/A2rchi.git /opt/A2rchi

# Install A2RCHI dependencies
RUN pip install -r /opt/A2rchi/requirements/requirements-base.txt && \
    pip install -r /opt/A2rchi/requirements/cpu-requirementsHEADER.txt

# Install A2RCHI (editable)
RUN pip install -e /opt/A2rchi

# Set working directory
WORKDIR /opt/A2rchi

# Create a startup script that handles the chat service
RUN echo '#!/bin/bash\n\
set -e\n\
echo "Starting A2RCHI chat service..."\n\
exec python src/bin/service_chat.py\n\
' > /opt/A2rchi/start-chat.sh && chmod +x /opt/A2rchi/start-chat.sh

# Default command - start the chat service
CMD ["/opt/A2rchi/start-chat.sh"]
