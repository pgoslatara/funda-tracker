import psycopg

CONNECTION = None


def get_database_connection(
    db_name="", db_user="", db_password="", db_host="", db_port=5432
):
    global CONNECTION
    if CONNECTION:
        return CONNECTION
    else:
        try:
            CONNECTION = psycopg.connect(
                dbname=db_name,
                user=db_user,
                password=db_password,
                host=db_host,
                port=db_port,
            )
            CONNECTION.autocommit = True
            return CONNECTION

        except psycopg.OperationalError as e:
            print(f"Encountered error: {e}")
            return None


def db_setup(table, schema, conn):
    cursor = conn.cursor()

    # Create the table if it doesn't exist yet.
    create_query = f"""
        CREATE TABLE IF NOT EXISTS {table}({", ".join([f"{k} {v}" for (k, v) in schema.items()])})
    """
    cursor.execute(create_query)

    # Idempotently migrate existing tables: add any columns from the schema that
    # aren't present yet (e.g. bag_bouwjaar on a pre-existing table). Strip the
    # PRIMARY KEY clause so ADD COLUMN doesn't try to add a second primary key.
    for column, column_type in schema.items():
        column_type_no_pk = column_type.replace("PRIMARY KEY", "").strip()
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type_no_pk}"
        )
