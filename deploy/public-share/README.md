# Public one-record share gateway

Production origin: `https://share.example.com`.

Boundary:

- Each tenant viewer writes selected exports to its own `PUBLIC_SHARE_STORE`.
- The sample `public-share-gateway` mounts one tenant export store read-only and keeps
  device-claim credentials in a separate writable gateway state directory.
- The gateway has no archive mount, private viewer URL, list/search/admin route, host port, or Docker socket.
- The ingress process joins `public-share-edge` and routes only `share.example.com` to
  `public-share-gateway:8000`. It must not route the private viewer.

Required private viewer configuration:

- `PUBLIC_SHARE_ENABLED=1` and a unique `TENANT_ID` matching a configured gateway store.
- `PUBLIC_SHARE_SECRET_FILE`: distinct high-entropy secret file, supplied outside git.
- `PUBLIC_SHARE_STORE_HOST`: dedicated host directory shared only with the gateway.
- `PUBLIC_SHARE_BASE_URL=https://share.example.com`.

Gateway configuration:

- `VIEWER_IMAGE`: immutable reviewed image tag.
- The sample Compose file declares one dedicated host export directory and one
  mode-0400 tenant secret file. Duplicate the matching mount, secret and
  `PUBLIC_SHARE_STORES` entry for each additional tenant.
- `PUBLIC_SHARE_GATEWAY_STATE`: dedicated writable directory owned by UID/GID
  10000. It contains only hashed device credentials and claim timestamps.
- `PUBLIC_SHARE_BASE_URL=https://share.example.com`. Missing or invalid configuration fails closed.

Security contract:

- URL shape is `<PUBLIC_SHARE_BASE_URL>/<opaque-id>#<secret>`.
- The first valid fragment exchange atomically consumes the claim and creates a
  new random HttpOnly Secure SameSite=Strict device cookie scoped to
  `/<opaque-id>`; the fragment is immediately removed from browser history.
- The same browser may reopen the share. A second browser receives the same
  opaque denial as an invalid, expired, or revoked share. Expiry and revocation
  invalidate the device cookie. This binds a device, not a verified identity.
- TTLs are exactly 1 hour, 24 hours, or 7 days. Default UI choice is 24 hours.
- Only a token HMAC and source-recording HMAC are stored. Export filenames contain only the opaque share ID.
- Revocation removes the exported files. The private viewer reconciles archive presence every 60 seconds and revokes shares for archived or deleted recordings.
- All public content, session, audio, and error responses are `no-store` with `no-referrer` and a restrictive CSP. Access logging is disabled for the gateway command.

Release procedure:

1. Build one immutable viewer image from the release commit. Use the exact same
   image for every viewer and the public gateway.
2. Create one export directory and one distinct secret file per tenant as
   UID/GID 10000, modes 0700 and 0400. Create the gateway state directory as
   UID/GID 10000, mode 0700. Never print secret contents.
3. Pin all store and secret-file paths, `PUBLIC_SHARE_GATEWAY_STATE`,
   `VIEWER_IMAGE`, and `PUBLIC_SHARE_BASE_URL` in the gateway environment.
4. Join only the ingress service to `public-share-edge`, then add only the
   `share.example.com` DNS record. Do not replace the zone or change the root redirect.
5. Verify external HTTPS, fragment removal, scoped cookie, Range audio, opaque
   invalid/expired/revoked parity, owner create/revoke, and private API isolation.

Rollback: keep the previous immutable image reference. Re-pin both the private
viewer and gateway to that reference and recreate them. Public exports created
by the newer release remain isolated in the export store; revoke them before
rollback if the older private viewer cannot administer them.
