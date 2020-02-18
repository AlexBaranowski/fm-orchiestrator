# -*- mode: ruby -*-
# vi: set ft=ruby ts=2 sw=2 ai et:

$script = <<SCRIPT
    set -e
    grep -q '^127\.0\.0\.1 fedmsg-relay$' /etc/hosts || echo "127.0.0.1 fedmsg-relay" >> /etc/hosts
    echo "export MODULE_BUILD_SERVICE_DEVELOPER_ENV=1" > /etc/profile.d/module_build_service_developer_env.sh
    source /etc/profile.d/module_build_service_developer_env.sh
    dnf install -y \
      bash-completion \
      python3-flake8 \
      python3-mock \
      python3-pytest \
      python3-pytest-cov \
      python3-tox \
      rpm-build \
      sqlite
    # Install the runtime dependencies from the module-build-service spec file
    curl -s https://src.fedoraproject.org/rpms/module-build-service/raw/master/f/module-build-service.spec -o /tmp/module-build-service.spec
    dnf install -y $(rpmspec --parse /tmp/module-build-service.spec  | grep ^Requires: | tr -s ' ' | cut -d ' ' -f2)
    mbs_config_dir=/etc/module-build-service
    [ -e "$mbs_config_dir" ] || mkdir "$mbs_config_dir"
    cd /opt/module_build_service
    cp -r conf/* "$mbs_config_dir"

    # Workaround because python3-koji has no egg-info file
    sed -i '/koji/d' requirements.txt
    # Remove Python 2 only dependencies
    sed -i '/futures/d' requirements.txt
    sed -i '/enum34/d' requirements.txt

    python3 setup.py develop --no-deps
    python3 setup.py egg_info
SCRIPT

$make_devenv = <<DEVENV
  set -e
  code_dir=/opt/module_build_service
  if ! grep "^cd $code_dir" ~/.bashrc >/dev/null; then
      # Go to working directory after login
      echo "cd $code_dir" >> ~/.bashrc
  fi
DEVENV

$config_pgsql = <<PGSQL
dnf install -y postgresql postgresql-server python3-psycopg2

pg_hba_conf=/var/lib/pgsql/data/pg_hba.conf

if [[ ! -e "$pg_hba_conf" ]]; then
  postgresql-setup --initdb
fi

systemctl start postgresql
systemctl enable postgresql

cp "$pg_hba_conf" "${pg_hba_conf}.orig"

# Allow to connect to PostgreSQL without password
if ! grep "host all all 127.0.0.1/32 trust" "$pg_hba_conf" >/dev/null; then
  echo "host all all 127.0.0.1/32 trust" > "$pg_hba_conf"
fi

# Avoid SQL query statement being truncated in pg_stat_activity, which is
# convenient for debugging.
pg_conf=/var/lib/pgsql/data/postgresql.conf
if ! grep "track_activity_query_size = 4096" "$pg_conf" >/dev/null; then
  echo "track_activity_query_size = 4096" >> "$pg_conf"
fi

# Restart to apply configuration changes
systemctl restart postgresql

psql -U postgres -h 127.0.0.1 -c "DROP DATABASE IF EXISTS mbstest"
psql -U postgres -h 127.0.0.1 -c "CREATE DATABASE mbstest"

echo "******** Run Tests with PostgreSQL ********"
echo "Set this environment variable to test with PostgreSQL"
echo "export DATABASE_URI=postgresql+psycopg2://postgres:@127.0.0.1/mbstest"
echo
PGSQL


Vagrant.configure("2") do |config|
  config.vm.box = "fedora/31-cloud-base"
  config.vm.synced_folder "./", "/opt/module_build_service", type: "rsync"
  # Disable the default share
  config.vm.synced_folder ".", "/vagrant", disabled: true
  config.vm.network "forwarded_port", guest_ip: "0.0.0.0", guest: 5000, host: 5000
  config.vm.provision "shell", inline: $script
  config.vm.provision "shell", inline: "usermod -a -G mock vagrant"
  config.vm.provision "shell", inline: $config_pgsql
  config.vm.provision "shell", inline: $make_devenv, privileged: false
  # 4096 and 4 cpu should be enough to build most packages
  config.vm.provider "libvirt" do |v, override|
    v.memory = 4096
    v.cpus = 4
  end
end
