-- 1. Habilitar la extensión para búsquedas rápidas por coincidencia parcial (ilike)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. Crear índices GIN sobre las columnas de idioma
CREATE INDEX IF NOT EXISTS idx_entries_en_trgm ON entries USING gin (en gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entries_es_trgm ON entries USING gin (es gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entries_ja_trgm ON entries USING gin (ja gin_trgm_ops);

-- 3. Crear índice para acelerar el filtro por archivo e id
CREATE INDEX IF NOT EXISTS idx_entries_file_id ON entries (file, entry_id);