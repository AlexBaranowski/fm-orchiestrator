#!/bin/bash
#
# Run MBS unit tests matrix
#     | SQLite   |   PostgreSQL
# ------------------------------
# py2 | x        |  x
# py3 | x        |  x
#
# Command line options:
# --py3: run tests inside mbs-test-fedora container with Python 3. If not
#        set, tests will run in mbs-test-centos with Python 2 by default.
# --with-pgsql: run tests with PostgreSQL, otherwise SQLite is used.
# --no-tty: don't use tty for containers
# --sudo: run Docker via sudo
# --podman: use Podman instead of Docker
# --no-pull: don't update Docker images
#
# Please note that, both of them can have arbitrary value as long as one of
# them is set. So, generally, it works by just setting to 1 or yes for
# simplicity.

enable_py3=
with_pgsql=
no_tty=
use_sudo=
use_podman=
do_pull=1

while (( "$#" )); do
    case "$1" in
        --py3) enable_py3=1 ;;
        --with-pgsql) with_pgsql=1 ;;
        --no-tty) no_tty=1 ;;
        --sudo) use_sudo=1 ;;
        --podman) use_podman=1 ;;
        --no-pull) do_pull= ;;
        *) break ;;
    esac
    shift
done

image_ns=quay.io/factory2
postgres_image="postgres:9.5.17"
test_container_name="mbs-test"
db_container_name="mbs-test-db"
source_dir="$(realpath "$(dirname "$0")")"
volume_mount="${source_dir}:/src:z"
db_name=mbstest
db_password=mbstest
pgdb_uri="postgresql+psycopg2://postgres:${db_password}@db/${db_name}"
db_bg_container=

if [ -n "$enable_py3" ]; then
    test_image="${image_ns}/mbs-test-fedora"
    test_container_name="${test_container_name}-py3"
    db_container_name="${db_container_name}-py3"
else
    test_image="${image_ns}/mbs-test-centos"
fi

if [ -n "$with_pgsql" ]; then
    test_container_name="${test_container_name}-pgsql"
fi
if [ -n "$use_podman" ]; then
    docker="podman"
elif [ -n "$use_sudo" ]; then
    # use sudo for docker
    docker="sudo /usr/bin/docker"
else
    docker="docker"
fi

now=$(date +"%H%M%S")
db_container_name="${db_container_name}-$now"
test_container_name="${test_container_name}-$now"

container_opts=(--rm -v "${volume_mount}" --name "$test_container_name")
container_opts+=(-e PATH=/src:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin)

if [ -z "$no_tty" ]; then
    container_opts+=(-i -t)
fi

if [ -n "$with_pgsql" ]; then
    container_opts+=(--link "${db_container_name}":db -e "DATABASE_URI=$pgdb_uri")

    # Database will be generated automatically by postgres container during launch.
    # Setting this password makes it possible to get into database container
    # and check the data.
    db_bg_container=$(
        $docker run --rm --name "$db_container_name" \
            -e POSTGRES_PASSWORD=$db_password \
            -e POSTGRES_DB=$db_name \
            -d \
            $postgres_image
    )

    # Waiting for postgres container to start completely in case tests start too fast.
    while true
    do
        if $docker exec "$db_bg_container" psql -U postgres -c '\dp' $db_name >/dev/null 2>&1; then
            break
        fi
    done
fi

tox_args=$@
if [[ $tox_args == tests/* ]]; then
    tox_args="/root/mbs/src/${tox_args}"
fi

[ -n "$do_pull" ] && $docker pull "$test_image"
(cd "$source_dir" && $docker run "${container_opts[@]}" $test_image "$tox_args")

rv=$?

[ -n "$db_bg_container" ] && $docker stop "$db_bg_container"
exit $rv

