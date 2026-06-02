import sqlite3

class URLDatabase:
    def __init__(self,db_path='urls.db'):
        self.db_path=db_path
        self._init_db()
    
    def _init_db(self):
        #creating table where id is primary key
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS urls(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                long_url TEXT NOT NULL,
                clicks INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
    

    def insert(self, long_url:str):
        """Inserts a new long URL into the database
        Return's the auto-generated ID
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor=conn.cursor()

            cursor.execute("INSERT INTO urls (long_url) VALUES (?)",(long_url,))
            return cursor.lastrowid
    
    def get_by_id(self,db_id:int) -> str | None:
        """Retrieves long url using the database row ID """
        with sqlite3.connect(self.db_path) as conn:
            cursor=conn.cursor()

            cursor.execute("SELECT long_url FROM urls WHERE id=? ", (db_id,))
            row=cursor.fetchone()
            return row[0] if row else None
    