import os
import psycopg2

c = psycopg2.connect(host='127.0.0.1', port=55432, user='f2e2e',
                     dbname='postgres', password=os.environ['PGPASSWORD'])
c.autocommit = True
cur = c.cursor()
cur.execute('select datname from pg_database order by datname')
print('DBS:', [r[0] for r in cur.fetchall()])
cur.execute("select 1 from pg_database where datname = 'p0c1e2e_20261004'")
if cur.fetchone():
    print('TARGET-DB: EXISTS')
else:
    cur.execute('CREATE DATABASE p0c1e2e_20261004')
    print('TARGET-DB: CREATED')
c.close()
