import json
import os
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

def migrate():
    load_dotenv()
    
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL is not set in .env")
        return

    json_file = Path("data/residents.json")
    if not json_file.exists():
        print(f"Error: {json_file} does not exist.")
        return
        
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading JSON: {e}")
        return
        
    if not data:
        print("No residents found in JSON to migrate.")
        return

    print(f"Connecting to database...")
    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return
        
    try:
        with conn.cursor() as cur:
            # Create table just in case it doesn't exist
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
            
            imported = 0
            for plate, res in data.items():
                try:
                    cur.execute("""
                        INSERT INTO resident_vehicles 
                        (plate_number, resident_name, flat_number, block, phone, vehicle_type, vehicle_model, vehicle_color, status, notes, added_on)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (plate_number) DO NOTHING
                    """, (
                        res.get("plate_number", plate),
                        res.get("resident_name", "Unknown"),
                        res.get("flat_number", ""),
                        res.get("block", ""),
                        res.get("phone", ""),
                        res.get("vehicle_type", "Car"),
                        res.get("vehicle_model", ""),
                        res.get("vehicle_color", ""),
                        res.get("status", "KNOWN"),
                        res.get("notes", ""),
                        res.get("added_on", "")
                    ))
                    imported += 1
                except Exception as e:
                    print(f"Failed to insert {plate}: {e}")
            
            conn.commit()
            print(f"Successfully migrated {imported} residents to PostgreSQL.")
            
    except Exception as e:
        print(f"Database error during migration: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()