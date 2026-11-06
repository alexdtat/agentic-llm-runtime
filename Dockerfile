# Reuse the cached Ubuntu image. All probes run with runc, without GPUs.
FROM nvidia/cuda@sha256:2ab6381d970b211fb93853796dc6707eb8a72575a375c422b17cf4d8b2641701
ENV DEBIAN_FRONTEND=noninteractive NVIDIA_VISIBLE_DEVICES=void
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 xvfb openbox xdotool x11-utils imagemagick inkscape \
    dbus-x11 fonts-dejavu-core sshfs openssh-client openssh-server iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /probe
CMD ["sleep", "infinity"]
