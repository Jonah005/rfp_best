# BidReady frontend

The implemented operator UI covers all ten locked screens with Next.js 15,
React 19, strict TypeScript and TanStack Query.

```bash
cp .env.local.example .env.local  # PowerShell: Copy-Item .env.local.example .env.local
pnpm install
pnpm dev                          # http://localhost:3000
```

Use the seeded development account `admin@acme-cyber.example.com` with password
`ChangeMe-Dev-2026`. The API defaults to `http://localhost:8000/api/v1`.

Quality gates: `pnpm typecheck`, `pnpm lint`, and `pnpm build`.
