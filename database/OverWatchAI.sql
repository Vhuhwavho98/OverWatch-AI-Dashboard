CREATE DATABASE OverWatchAI;
GO
USE OverWatchAI;
CREATE TABLE detections(
id INT IDENTITY PRIMARY KEY,
plate VARCHAR(20),
vehicle_type VARCHAR(50),
overload_status VARCHAR(20),
confidence FLOAT,
location VARCHAR(100),
created_at DATETIME DEFAULT GETDATE());