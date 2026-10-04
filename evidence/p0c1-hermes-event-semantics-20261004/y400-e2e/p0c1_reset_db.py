import os
import psycopg2

c = psycopg2.connect(host='127.0.0.1', port=55432, user='f2e2e',
                     dbname='postgres', password=os.environ['PGPASSWORD'])
c.autocommit = True
cur = c.cursor()
cur.execute('DROP DATABASE IF EXISTS p0c1e2e_20261004 WITH (FORCE)')
cur.execute('CREATE DATABASE p0c1e2e_20261004')
print('RESET-OK')
c.close()
