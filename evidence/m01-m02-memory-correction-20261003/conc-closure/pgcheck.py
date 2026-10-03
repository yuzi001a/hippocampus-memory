import pathlib, sys
import psycopg2
sec = pathlib.Path(r"C:/hp-testbed/f2-overnight-20261002/secrets/pgpw.tmp").read_text(encoding="utf-8").strip()
c = psycopg2.connect(host="127.0.0.1", port=55432, dbname="postgres", user="f2e2e",
                    password=sec, connect_timeout=10)
c.autocommit = True
with c.cursor() as cur:
    cur.execute("select current_database(), current_user, inet_server_addr()::text, inet_server_port(), version()")
    print("CONN", cur.fetchone())
    cur.execute("select datname from pg_database where datname like 'm01m02e2e%' order by datname")
    print("EXISTING_M01M02", [r[0] for r in cur.fetchall()])
    cur.execute("select count(*) from pg_database")
    print("DB_COUNT", cur.fetchone()[0])
    cur.execute("select pid, datname, state from pg_stat_activity where datname is not null order by pid")
    print("ACTIVITY", cur.fetchall())
c.close()
