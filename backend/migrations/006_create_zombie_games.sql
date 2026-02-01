-- Migration: Create zombie_games table
-- Date: 2026-02-01
-- Description: Add zombie survival game tracking

CREATE TABLE IF NOT EXISTS zombie_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kills INTEGER NOT NULL DEFAULT 0,
    wave INTEGER NOT NULL DEFAULT 1,
    accuracy REAL DEFAULT 0.0,
    duration INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_zombie_kills ON zombie_games(kills DESC);
CREATE INDEX IF NOT EXISTS idx_zombie_user ON zombie_games(user_id);
CREATE INDEX IF NOT EXISTS idx_zombie_created ON zombie_games(created_at DESC);
