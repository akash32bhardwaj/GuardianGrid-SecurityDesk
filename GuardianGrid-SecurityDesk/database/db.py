import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    """
    Returns PostgreSQL connection.
    """
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not found in .env")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Initialize the database tables if they don't exist."""
    if not DATABASE_URL:
        return
    
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS resident_vehicles (
                    plate_number VARCHAR(50) PRIMARY KEY,
                    resident_name VARCHAR(255) NOT NULL,
                    flat_number VARCHAR(50),
                    block VARCHAR(50),
                    phone VARCHAR(50),
                    vehicle_type VARCHAR(50),
                    vehicle_model VARCHAR(100),
                    vehicle_color VARCHAR(50),
                    status VARCHAR(50),
                    notes TEXT,
                    added_on VARCHAR(50)
                );
            """)
        conn.commit()
    except Exception as e:
        print(f"Error initializing DB: {e}")
    finally:
        conn.close()