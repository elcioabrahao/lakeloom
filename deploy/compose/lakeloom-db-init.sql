-- DEVELOPMENT ONLY. Mounted into the OpenMetadata postgres container as
-- /docker-entrypoint-initdb.d/zz-lakeloom.sql; runs once, on an empty data volume.
-- Lakeloom state store (Task 8): postgresql://lakeloom:lakeloom@postgresql:5432/lakeloom
CREATE USER lakeloom WITH PASSWORD 'lakeloom';
CREATE DATABASE lakeloom OWNER lakeloom;
