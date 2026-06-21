-- =====================================================
-- GuardianGrid Platform Database Schema V1
-- =====================================================

-- SOCIETIES
CREATE TABLE societies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    address TEXT,
    contact_person VARCHAR(255),
    phone VARCHAR(50),
    email VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- USERS
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- OPERATORS
CREATE TABLE operators (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(50),
    status VARCHAR(50) DEFAULT 'ACTIVE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- RESIDENTS
CREATE TABLE residents (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    resident_name VARCHAR(255) NOT NULL,
    flat_number VARCHAR(50),
    block VARCHAR(50),
    phone VARCHAR(50),
    status VARCHAR(50),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- VEHICLES
CREATE TABLE vehicles (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    resident_id INTEGER REFERENCES residents(id),
    plate_number VARCHAR(50) UNIQUE NOT NULL,
    vehicle_type VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- VEHICLE EVENTS
CREATE TABLE vehicle_events (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    plate_number VARCHAR(50),
    event_type VARCHAR(20),
    confidence NUMERIC(5,2),
    camera_name VARCHAR(255),
    snapshot_path TEXT,
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- THREAT EVENTS
CREATE TABLE threat_events (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    threat_type VARCHAR(100),
    severity VARCHAR(50),
    camera_name VARCHAR(255),
    snapshot_path TEXT,
    description TEXT,
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- INCIDENTS
CREATE TABLE incidents (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    incident_type VARCHAR(100),
    severity VARCHAR(50),
    camera_name VARCHAR(255),
    status VARCHAR(50) DEFAULT 'OPEN',
    snapshot_path TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP
);

-- INCIDENT ACTIONS
CREATE TABLE incident_actions (
    id SERIAL PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id),
    operator_id INTEGER REFERENCES operators(id),
    action_taken TEXT,
    action_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- CAMERAS
CREATE TABLE cameras (
    id SERIAL PRIMARY KEY,
    society_id INTEGER REFERENCES societies(id),
    camera_name VARCHAR(255),
    location VARCHAR(255),
    rtsp_url TEXT,
    status VARCHAR(50) DEFAULT 'ACTIVE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);