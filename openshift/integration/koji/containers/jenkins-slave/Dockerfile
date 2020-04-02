# Based on the rad-jenkins image, which is in turn based on:
# https://github.com/jenkinsci/docker-jnlp-slave/blob/master/Dockerfile
# https://github.com/jenkinsci/docker-slave/blob/master/Dockerfile

FROM fedora:31
LABEL \
    org.opencontainers.image.title="Jenkins slave for Module Build Service (MBS) pipelines" \
    org.opencontainers.image.description="The MBS coordinates module builds. This image is to serve as the slave for executing build and test pipelines." \
    org.opencontainers.image.vendor="The Factory 2.0 Team" \
    org.opencontainers.image.authors="The Factory 2.0 Team <pnt-factory2-devel@redhat.com>" \
    org.opencontainers.image.licenses="GPLv2+" \
    org.opencontainers.image.url="https://pagure.io/fm-orchestrator" \
    org.opencontainers.image.documentation="https://pagure.io/fm-orchestrator" \
    distribution-scope="private"

ARG USER=jenkins
ARG UID=10000
ARG HOME_DIR=/home/jenkins
ARG REMOTING_VERSION=3.35
ARG TINI_VERSION=0.18.0
ARG DNF_CMD="dnf -y --setopt=deltarpm=0 --setopt=install_weak_deps=false --setopt=tsflags=nodocs"
ARG CA_URLS=""

# Provide a default HOME location and set some default git user details
# Set LANG to UTF-8 to support it in stdout/stderr
ENV HOME=${HOME_DIR} \
    GIT_COMMITTER_NAME="The Factory 2.0 Team" \
    GIT_COMMITTER_EMAIL=pnt-factory2-devel@redhat.com \
    LANG=en_US.UTF-8

USER root

RUN ${DNF_CMD} install -y \
    java-1.8.0-openjdk nss_wrapper gettext git jq \
    tar gzip skopeo wget make bind-utils python3-jinja2-cli \
    origin-clients \
    # Jenkins pipeline 'sh' steps seem to require ps
    procps-ng \
    # Tools to interface with our test instances
    koji krb5-workstation && \
    ${DNF_CMD} clean all

# CA Certs
WORKDIR /etc/pki/ca-trust/source/anchors/
RUN for ca_url in ${CA_URLS}; do curl -skO ${ca_url}; done && \
    update-ca-trust

# Setup the user for non-arbitrary UIDs with OpenShift
# https://docs.openshift.org/latest/creating_images/guidelines.html#openshift-origin-specific-guidelines
RUN useradd -d ${HOME_DIR} -u ${UID} -g 0 -m -s /bin/bash ${USER} && \
    chmod -R g+rwx ${HOME_DIR}

# Make /etc/passwd writable for root group
# so we can add dynamic user to the system in entrypoint script
RUN chmod g+rw /etc/passwd

# Retrieve jenkins slave client
RUN curl --create-dirs -sSLo /usr/share/jenkins/slave.jar \
    https://repo.jenkins-ci.org/public/org/jenkins-ci/main/remoting/${REMOTING_VERSION}/remoting-${REMOTING_VERSION}.jar && \
    chmod 755 /usr/share/jenkins && \
    chmod 644 /usr/share/jenkins/slave.jar

# Entry point script to run jenkins slave client
COPY jenkins-slave /usr/local/bin/jenkins-slave
RUN chmod 755 /usr/local/bin/jenkins-slave

# install tini, a tiny but valid init for containers
# install wait-for-it.sh, to allow containers to wait for other services to come up
RUN curl -L -o /usr/local/bin/tini "https://github.com/krallin/tini/releases/download/v${TINI_VERSION}/tini" \
    && chmod +rx /usr/local/bin/tini \
    && curl -L -o /usr/local/bin/wait-for-it "https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh" \
    && chmod +rx /usr/local/bin/tini /usr/local/bin/wait-for-it \
    && ${DNF_CMD} clean all

# For OpenShift we MUST use the UID of the user and not the name.
USER ${UID}
WORKDIR ${HOME_DIR}
ENTRYPOINT ["/usr/local/bin/tini", "--", "jenkins-slave"]
