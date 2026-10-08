-- Only hashes of opaque browser credentials are retained. Never store IdP tokens.
CREATE TABLE auth_login_states (
    state_hash TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL,
    verifier TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE auth_sessions (
    session_hash TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    authenticated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX auth_sessions_subject ON auth_sessions(issuer,subject);
CREATE TABLE auth_revoked_subjects (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    revoked_at INTEGER NOT NULL,
    PRIMARY KEY (issuer,subject)
);
