#!/usr/bin/env bash
# delega-db: database engines inside a kindling microVM (runs as root; the image has no systemd).
#
#   delega-db install <engine> [version]            in the golden build (needs internet egress)
#   delega-db has <engine> [version]                exit 0 if the engine is installed
#   delega-db up <engine> <name> <database> [seed]  init + load the seed once + start (idempotent)
#   delega-db check <engine> <name> <database>      exit 0 if it answers; prints row/key counts only
#   delega-db stop <engine> <name>
#   delega-db manifest <seed-dir>                   the seed's MANIFEST (KEY=VALUE, no data)
#
# engine: postgres | mariadb | mysql | redis | mongo | sqlite. Data lives in /var/lib/delega-db/<name>
# (the VM's own disk, a copy-on-write of its template): the seed volume is mounted read-only and is
# never written. A seed dir holds dump.<ext> or dump.<ext>.part-* (concatenated in order) and MANIFEST.
# Formats: postgres .sql .sql.gz .dump (pg_dump -Fc) .tar · mariadb/mysql .sql .sql.gz · redis .rdb
# .redis (commands, redis-cli --pipe) · mongo .archive .archive.gz · sqlite .db .sqlite .sqlite3 .sql.
# Connection: 127.0.0.1, user delega, password delega (throwaway, only reachable inside the VM).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ROOT=/var/lib/delega-db
OPT=/opt/delega-db
U=delega PW=delega

die() { echo "delega-db: $*" >&2; exit 1; }
port() { case $1 in postgres) echo 5432;; mariadb|mysql) echo 3306;; redis) echo 6379;; mongo) echo 27017;; *) echo 0;; esac; }
apt_in() { apt-get update -qq && apt-get install -y -qq --no-install-recommends "$@" >/dev/null; }
pgbin() { ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1; }

# The seed as one stream on stdout (parts concatenated); $2 = extension found.
seed_file() {
  local d=$1 f
  for f in "$d"/dump.*; do
    [ -e "$f" ] || continue
    case $f in *.part-*) echo "${f%.part-*}"; return;; *MANIFEST*) ;; *) echo "$f"; return;; esac
  done
}
seed_cat() { if [ -e "$1" ]; then cat "$1"; else cat "$1".part-*; fi; }

install() {
  local e=$1 v=${2:-}
  case $e in
    postgres)
      if [ -n "$v" ] && [ ! -d /usr/lib/postgresql/"$v" ] && [ "$v" != 15 ]; then
        apt_in ca-certificates curl gnupg
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor --yes -o /usr/share/keyrings/pgdg.gpg
        echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
          > /etc/apt/sources.list.d/pgdg.list
        apt_in "postgresql-$v"
      else
        apt_in postgresql
      fi ;;
    mariadb) apt_in mariadb-server mariadb-client ;;
    mysql)
      [ -n "$v" ] || die "mysql needs a full version (e.g. 8.4.6)"
      apt_in ca-certificates curl xz-utils libaio1 libnuma1 libncurses5 libtinfo5
      mkdir -p "$OPT" && curl -fsSL "https://cdn.mysql.com/archives/mysql-${v%.*}/mysql-$v-linux-glibc2.17-x86_64-minimal.tar.xz" \
        | tar -xJ -C "$OPT" && rm -rf "$OPT/mysql" && mv "$OPT/mysql-$v-"* "$OPT/mysql"
      id mysql >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin mysql ;;
    redis) apt_in redis-server ;;
    mongo)
      [ -n "$v" ] || die "mongo needs a full version (e.g. 7.0.14)"
      apt_in ca-certificates curl libcurl4 openssl
      mkdir -p "$OPT/mongo"
      curl -fsSL "https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian12-$v.tgz" | tar -xz -C "$OPT/mongo" --strip-components=1
      curl -fsSL "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-debian12-x86_64-100.10.0.tgz" \
        | tar -xz -C "$OPT/mongo" --strip-components=1
      id mongodb >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin mongodb ;;
    sqlite) apt_in sqlite3 ;;
    *) die "unknown engine '$e'" ;;
  esac
  has "$e" "$v" || die "$e $v did not install"
  echo "installed $e ${v:-default}"
}

has() {
  local e=$1 v=${2:-}
  case $e in
    postgres) if [ -n "$v" ]; then [ -x /usr/lib/postgresql/"$v"/bin/postgres ]; else [ -n "$(pgbin)" ]; fi ;;
    mariadb) command -v mariadbd >/dev/null ;;
    mysql) [ -x "$OPT/mysql/bin/mysqld" ] ;;
    redis) command -v redis-server >/dev/null ;;
    mongo) [ -x "$OPT/mongo/bin/mongod" ] && [ -x "$OPT/mongo/bin/mongorestore" ] ;;
    sqlite) command -v sqlite3 >/dev/null ;;
    *) return 1 ;;
  esac
}

wait_for() { local _; for _ in $(seq 1 120); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 0.5; done; return 1; }

my_bin() { if [ "$1" = mysql ]; then echo "$OPT/mysql/bin"; else dirname "$(command -v mariadbd)"; fi; }
my_cli() { local b; b=$(my_bin "$1"); if [ -x "$b/mariadb" ]; then echo "$b/mariadb"; elif [ -x "$b/mysql" ]; then echo "$b/mysql"; else command -v mariadb; fi; }

running() {
  local e=$1 n=$2 d=$ROOT/$2
  case $e in
    postgres) runuser -u postgres -- "$(pgbin)/pg_ctl" -D "$d/data" status >/dev/null 2>&1 ;;
    mariadb|mysql) [ -S "$d/sock" ] && "$(my_cli "$e")" --no-defaults --socket="$d/sock" -uroot -e 'select 1' >/dev/null 2>&1 ;;
    redis) redis-cli -p "$(port redis)" ping 2>/dev/null | grep -q PONG ;;
    mongo) "$OPT/mongo/bin/mongod" --version >/dev/null && [ -f "$d/data/mongod.lock" ] && [ -s "$d/data/mongod.lock" ] ;;
    sqlite) [ -f "$d/$n.sqlite" ] ;;
  esac
}

start() {
  local e=$1 n=$2 d=$ROOT/$2 p; p=$(port "$1")
  case $e in
    postgres)
      runuser -u postgres -- "$(pgbin)/pg_ctl" -D "$d/data" -l "$d/log" -w -t 120 \
        -o "-c listen_addresses=127.0.0.1 -p $p -c unix_socket_directories=$d -c fsync=off -c full_page_writes=off" start >/dev/null ;;
    mariadb|mysql)
      local b; b=$(my_bin "$e")
      local srv=$b/mariadbd; [ -x "$srv" ] || srv=$b/mysqld
      local x=(); [ "$e" != mysql ] || x=(--mysqlx=OFF)
      setsid nohup "$srv" --no-defaults --basedir="${b%/bin}" --user=mysql --datadir="$d/data" --socket="$d/sock" --pid-file="$d/pid" --bind-address=127.0.0.1 \
        --port="$p" --skip-name-resolve "${x[@]}" --innodb-flush-log-at-trx-commit=0 >"$d/log" 2>&1 </dev/null &
      wait_for "$(my_cli "$e")" --no-defaults --socket="$d/sock" -uroot -e 'select 1' || die "$e did not start (see $d/log)" ;;
    redis)
      redis-server --bind 127.0.0.1 --port "$p" --dir "$d" --dbfilename dump.rdb --daemonize yes \
        --logfile "$d/log" --save "" --appendonly no >/dev/null
      wait_for sh -c "redis-cli -p $p ping | grep -q PONG" || die "redis did not start" ;;
    mongo)
      runuser -u mongodb -- "$OPT/mongo/bin/mongod" --dbpath "$d/data" --bind_ip 127.0.0.1 --port "$p" --fork \
        --logpath "$d/log" >/dev/null ;;
    sqlite) : ;;
  esac
}

# One-time: empty engine, the delega user/database, the seed loaded.
init_load() {
  local e=$1 n=$2 db=$3 seed=${4:-} d=$ROOT/$2 p f=""; p=$(port "$1")
  mkdir -p "$d"
  [ -z "$seed" ] || f=$(seed_file "$seed")
  [ -z "$seed" ] || [ -n "$f" ] || die "no dump.* in $seed"
  case $e in
    postgres)
      chown postgres: "$d"
      runuser -u postgres -- "$(pgbin)/initdb" -D "$d/data" -A trust -U postgres -E UTF8 >/dev/null
      start "$e" "$n"
      local psql; psql="$(pgbin)/psql -h 127.0.0.1 -p $p -U postgres -q"
      $psql -c "create role $U login superuser password '$PW'" -c "create database \"$db\" owner $U" >/dev/null
      if [ -n "$f" ]; then
        case $f in
          *.sql) seed_cat "$f" | $psql -d "$db" -o /dev/null 2>"$d/load.err" || true ;;
          *.sql.gz) seed_cat "$f" | gunzip | $psql -d "$db" -o /dev/null 2>"$d/load.err" || true ;;
          *.dump|*.tar) seed_cat "$f" | "$(pgbin)/pg_restore" -h 127.0.0.1 -p "$p" -U postgres --no-owner --role="$U" \
                          -d "$db" 2>"$d/load.err" || true ;;
          *) die "postgres: unknown dump format ${f##*/}" ;;
        esac
      fi ;;
    mariadb|mysql)
      local b; b=$(my_bin "$e"); chown mysql: "$d"
      if [ "$e" = mariadb ]; then
        mariadb-install-db --no-defaults --user=mysql --datadir="$d/data" --auth-root-authentication-method=socket >"$d/log" 2>&1 \
          || die "mariadb init failed (see $d/log)"
      else
        "$b/mysqld" --no-defaults --basedir="${b%/bin}" --initialize-insecure --user=mysql --datadir="$d/data" >"$d/log" 2>&1 || die "mysql init failed (see $d/log)"
      fi
      start "$e" "$n"
      local cli; cli="$(my_cli "$e") --no-defaults --socket=$d/sock -uroot"
      $cli -e "create database \`$db\`; create user '$U'@'%' identified by '$PW'; create user '$U'@'localhost' identified by '$PW';
               grant all on *.* to '$U'@'%'; grant all on *.* to '$U'@'localhost'; flush privileges;"
      if [ -n "$f" ]; then
        case $f in
          *.sql) seed_cat "$f" | $cli "$db" 2>"$d/load.err" || true ;;
          *.sql.gz) seed_cat "$f" | gunzip | $cli "$db" 2>"$d/load.err" || true ;;
          *) die "$e: unknown dump format ${f##*/}" ;;
        esac
      fi ;;
    redis)
      chown redis: "$d" 2>/dev/null || true
      case $f in
        "") ;;
        *.rdb) seed_cat "$f" > "$d/dump.rdb" ;;
        *.redis) ;;
        *) die "redis: unknown dump format ${f##*/}" ;;
      esac
      start "$e" "$n"
      case $f in *.redis) seed_cat "$f" | redis-cli -p "$p" --pipe >"$d/load.err" 2>&1 || true ;; esac ;;
    mongo)
      mkdir -p "$d/data" && chown -R mongodb: "$d"
      start "$e" "$n"
      case $f in
        "") ;;
        *.archive) seed_cat "$f" | "$OPT/mongo/bin/mongorestore" --port "$p" --archive --drop --quiet 2>"$d/load.err" || true ;;
        *.archive.gz) seed_cat "$f" | "$OPT/mongo/bin/mongorestore" --port "$p" --archive --gzip --drop --quiet 2>"$d/load.err" || true ;;
        *) die "mongo: unknown dump format ${f##*/}" ;;
      esac ;;
    sqlite)
      case $f in
        "") sqlite3 "$d/$n.sqlite" 'pragma user_version' >/dev/null ;;
        *.db|*.sqlite|*.sqlite3) seed_cat "$f" > "$d/$n.sqlite" ;;
        *.sql) seed_cat "$f" | sqlite3 "$d/$n.sqlite" 2>"$d/load.err" || true ;;
        *) die "sqlite: unknown dump format ${f##*/}" ;;
      esac
      chmod 666 "$d/$n.sqlite"; chmod 777 "$d" ;;
    *) die "unknown engine '$e'" ;;
  esac
  [ -z "$seed" ] || cp "$seed/MANIFEST" "$d/SEED" 2>/dev/null || true
  touch "$d/.loaded"
  # Load errors (missing roles, extensions…) are counted, never printed: they can quote data.
  local errs=0; [ ! -s "$d/load.err" ] || errs=$(grep -ci 'error' "$d/load.err" || true)
  echo "loaded $e/$n errors=$errs"
}

up() {
  local e=$1 n=$2 db=$3 seed=${4:-}
  has "$e" || die "$e is not installed in this image (delega golden build with the project's databases)"
  if [ -f "$ROOT/$n/.loaded" ]; then
    running "$e" "$n" || start "$e" "$n"
    echo "up $e/$n (already loaded)"
  else
    init_load "$e" "$n" "$db" "$seed"
  fi
}

check() {
  local e=$1 n=$2 db=$3 d=$ROOT/$2 p; p=$(port "$1")
  case $e in
    postgres) echo "tables=$("$(pgbin)/psql" -h 127.0.0.1 -p "$p" -U "$U" -d "$db" -Atc \
                "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')")" ;;
    mariadb|mysql) echo "tables=$("$(my_cli "$e")" --no-defaults --socket="$d/sock" -uroot -N -e \
                "select count(*) from information_schema.tables where table_schema='$db'")" ;;
    redis) echo "keys=$(redis-cli -p "$p" dbsize)" ;;
    mongo) echo "collections=$("$OPT/mongo/bin/mongodump" --port "$p" --db "$db" --archive=/dev/null 2>&1 | grep -c 'done dumping' || true)" ;;
    sqlite) echo "tables=$(sqlite3 "$d/$n.sqlite" "select count(*) from sqlite_master where type='table'")" ;;
  esac
}

stop() {
  local e=$1 n=$2 d=$ROOT/$2
  case $e in
    postgres) runuser -u postgres -- "$(pgbin)/pg_ctl" -D "$d/data" -m fast stop >/dev/null ;;
    mariadb|mysql) kill "$(cat "$d/pid")" ;;
    redis) redis-cli -p "$(port redis)" shutdown nosave >/dev/null ;;
    mongo) runuser -u mongodb -- "$OPT/mongo/bin/mongod" --dbpath "$d/data" --shutdown >/dev/null ;;
    sqlite) : ;;
  esac
}

cmd=${1:-}; shift || true
case $cmd in
  install) install "$@" ;;
  has) has "$@" ;;
  up) up "$@" ;;
  check) check "$@" ;;
  stop) stop "$@" ;;
  manifest) cat "$1/MANIFEST" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
