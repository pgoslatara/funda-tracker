def db_setup(table, schema, conn):
    query = f"""
        CREATE TABLE IF NOT EXISTS {table}({", ".join([f"{k} {v}" for (k,v) in schema.items()])})
    """

    conn.cursor().execute(query)
