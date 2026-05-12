
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS embeddings (
    unique_id TEXT PRIMARY KEY,
    sub_id TEXT,
    text TEXT,
    embedding vector(768)
);

CREATE INDEX IF NOT EXISTS embeddings_vector_idx
ON embeddings
USING ivfflat (embedding vector_l2_ops);
