# Same pinned base and dependency layer as the sandbox image. This makes the
# gateway build independent of a pre-existing local course-feasibility tag.
FROM nvidia/cuda@sha256:2ab6381d970b211fb93853796dc6707eb8a72575a375c422b17cf4d8b2641701
ENV DEBIAN_FRONTEND=noninteractive NVIDIA_VISIBLE_DEVICES=void
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 xvfb openbox xdotool x11-utils imagemagick inkscape \
    dbus-x11 fonts-dejavu-core sshfs openssh-client openssh-server iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY track_a/server.py track_a/worker.py /prototype/
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/prototype/server.py"]
