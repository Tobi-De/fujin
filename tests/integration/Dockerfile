FROM debian:12

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

# 1. Install systemd, ssh, sudo, python (required for fujin/uv)
RUN apt-get update && \
    apt-get install -y systemd systemd-sysv openssh-server sudo python3 curl git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. Configure SSH
RUN mkdir /var/run/sshd
# Allow root login for simplicity in tests, or create a user
RUN echo 'root:root' | chpasswd
RUN sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config

# 3. Create a deploy user
RUN useradd -m -s /bin/bash fujin && \
    echo "fujin:fujin" | chpasswd && \
    usermod -aG sudo fujin && \
    echo "fujin ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# 4. Systemd cleanup (standard steps to make systemd work in docker)
RUN systemctl enable ssh
RUN rm -f /lib/systemd/system/multi-user.target.wants/* \
    /etc/systemd/system/*.wants/* \
    /lib/systemd/system/local-fs.target.wants/* \
    /lib/systemd/system/sockets.target.wants/*udev* \
    /lib/systemd/system/sockets.target.wants/*initctl* \
    /lib/systemd/system/sysinit.target.wants/systemd-tmpfiles-setup* \
    /lib/systemd/system/systemd-update-utmp*

# 5. Set systemd as entrypoint
CMD ["/lib/systemd/systemd"]

# 6. Mock Caddy (so we don't need to install the full binary)
RUN mkdir -p /etc/caddy/conf.d
RUN echo '[Unit]\nDescription=Dummy Caddy\n[Service]\nExecStart=/bin/sleep infinity\nExecReload=/bin/true\n[Install]\nWantedBy=multi-user.target' > /etc/systemd/system/caddy.service
RUN systemctl enable caddy
