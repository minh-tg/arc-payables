-- State appears in redirects/logs; bind it to an independent HttpOnly browser secret.
-- Old outstanding sign-in requests are invalidated, not upgraded or trusted.
ALTER TABLE auth_login_states ADD COLUMN browser_hash TEXT;
