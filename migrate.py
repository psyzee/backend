import os, psycopg2
if __name__ == '__main__':
    conn_str = os.environ.get('DATABASE_URL')
    if not conn_str:
        print('Set DATABASE_URL to run migrations.')
        exit(1)
    sql_text = open('migrate.sql').read()
    with psycopg2.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            conn.commit()
    print('Migration applied.')
