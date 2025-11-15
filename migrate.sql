-- Create tokens table
CREATE TABLE IF NOT EXISTS tokens (
  id serial PRIMARY KEY,
  access_token text,
  refresh_token text,
  token_type varchar(64),
  expires_at double precision,
  created_at double precision DEFAULT extract(epoch from now())
);
