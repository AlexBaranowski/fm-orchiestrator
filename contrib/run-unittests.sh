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
#
# Please note that, both of them can have arbitrary value as long as one of
# them is set. So, generally, it works by just setting to 1 or yes for
# simplicity.

enable_py3=
with_pgsql=

for arg in "$@"; do
    case $arg in
        --py3) enable_py3=1 ;;
        --with-pgsql) with_pgsql=1 ;;
    esac
done

image_ns=quay.io/factory2
postgres_image="postgres:9.5.17"
db_container_name="mbs-test-db"
source_dir="$(realpath "$(dirname "$0")/..")"
volume_mount="${source_dir}:/src:Z"
db_name=mbstest
db_password=mbstest
pgdb_uri="postgresql+psycopg2://postgres:${db_password}@db/${db_name}"
db_bg_container=

if [ -n "$enable_py3" ]; then
    test_image="${image_ns}/mbs-test-fedora"
else
    test_image="${image_ns}/mbs-test-centos"
fi

container_opts=(--rm -i -t -v "${volume_mount}" --name mbs-test)

if [ -n "$with_pgsql" ]; then
    container_opts+=(--link "${db_container_name}":db -e "DATABASE_URI=$pgdb_uri")

    # Database will be generated automatically by postgres container during launch.
    # Setting this password makes it possible to get into database container
    # and check the data.
    db_bg_container=$(
        docker run --rm --name $db_container_name \
            -e POSTGRES_PASSWORD=$db_password \
            -e POSTGRES_DB=$db_name \
            -d \
            $postgres_image
    )
fi

(cd "$source_dir" && docker run "${container_opts[@]}" $test_image)

rv=$?

[ -n "$db_bg_container" ] && docker stop "$db_bg_container"
exit $rv

