import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

class URLDatabase:
    def __init__(self,db_url=os.getenv('DATABASE_URL')):
        self.db_url=db_url
        if not self.db_url:
            raise ValueError("DATABASE_URL environment variable is missing!")
        self._init_db()
    
    def _get_connection(self):
        return psycopg2.connect(self.db_url)
        #Postgres creates a fresh connection per request

    def _init_db(self):
        #creating table where id is primary key
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS urls(
                id SERIAL PRIMARY KEY,
                long_url TEXT NOT NULL,
                clicks INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
    

    def insert(self, long_url:str):
        """Inserts a new long URL into the database
        Return's the auto-generated ID
        """
        with self._get_connection() as conn:
            cursor=conn.cursor()

            cursor.execute("INSERT INTO urls (long_url) VALUES (%s) RETURNING id",(long_url,))
            new_id=cursor.fetchone()[0]
            conn.commit()
            return new_id
    
    def get_by_id(self,db_id:int) -> str | None:
        """Retrieves long url using the database row ID """
        with self._get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT long_url FROM urls WHERE id=%s ", (db_id,))
                row=cursor.fetchone()
                return row[0] if row else None
    