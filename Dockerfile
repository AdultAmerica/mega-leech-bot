# Debian 12 (bookworm) base with Python 3.12.
FROM python:3.12-slim-bookworm

# --- Install MEGAcmd from MEGA's official Debian package ---------------------
# If the Railway build fails on this step, the most likely cause is the .deb
# URL below: open https://mega.nz/cmd , find the "Debian 12" download link, and
# replace the URL with the current one. Everything else can stay the same.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && wget -q "https://mega.nz/linux/repo/Debian_12/amd64/megacmd-Debian_12_amd64.deb" \
        -O /tmp/megacmd.deb \
    && (apt-get install -y /tmp/megacmd.deb \
        || (apt-get -f install -y && apt-get install -y /tmp/megacmd.deb)) \
    && rm -f /tmp/megacmd.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies -----------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App code ----------------------------------------------------------------
COPY . .

CMD ["python", "bot.py"]
