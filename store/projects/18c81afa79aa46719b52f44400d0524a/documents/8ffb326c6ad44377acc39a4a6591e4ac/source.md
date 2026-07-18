# Task : Auth

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `02-user`

## Sequência de execução

1. Domínio: `AuthAccount`, tokens, lifecycle, regras de autenticação — com testes
2. Migration: schema de auth accounts e refresh tokens
3. Repositório: persistência e lookup de auth accounts e refresh tokens
4. Use cases: registro por password (user comum — requer autenticação), registro por password (doctor — cria User + AuthAccount + Doctor em uma transação, público), aceite de convite por password (attendant define senha via token), aceite de convite por OAuth (attendant conecta Google via token), login por password, OAuth via Google (`goth`), geração de access e refresh token, revogação de refresh token
5. Handler: endpoints de auth, middleware JWT global
   - `POST /auth/register` — `admin`, `attendant`, `doctor`
   - `POST /auth/register/doctor` — público (doctor: name, email, password, crm)
   - `POST /auth/login` — público
   - `POST /auth/oauth/google` — público
   - `POST /auth/refresh` — público
   - `POST /auth/logout` — autenticado
   - `POST /auth/invite/accept/password` — público (token de convite + senha)
   - `POST /auth/invite/accept/oauth` — público (token de convite + OAuth Google)
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: lookup de user por token)

## Restrições

- Médico pode se cadastrar mas só faz login após ativação pelo admin
- Patient não tem `AuthAccount` — não faz login
- OAuth disponível apenas para users com `AuthAccount` (`admin`, `attendant`, `doctor`)
- Attendant recebe convite por email ao ser vinculado por um doctor
- Convite tem token temporário — attendant escolhe senha ou OAuth para criar seu `AuthAccount`
- Access token expira em 15 minutos
- Refresh token expira em 7 dias

## Progresso

- [x] Domínio — 2026-06-25
      `AuthAccount` (email, password_hash, OAuth provider), `RefreshToken` (IsValid: revocação + expiração), `InviteToken` (IsValid: uso + expiração). Erros de domínio. 11 testes cobrindo invariantes.
- [x] Migration — 2026-06-25
      000002: auth_accounts, refresh_tokens, invite_tokens. 000003: doctors (PK = user_id, crm UNIQUE, doctor_status default 'pending').
- [x] Repositório — 2026-06-25
      infra/repository.go implementa as 3 interfaces. `RegisterDoctor` usa transação pgx (users + auth_accounts + doctors). Joins com users e doctors em FindAccountByEmail/ByProviderID.
- [x] Use cases — 2026-06-25
      token_generator (JWT HS256 15min + nanoid refresh 7d), register_by_password, register_doctor (tx), login_by_password (verifica doctor_status), refresh_token (rotação), revoke_token, accept_invite_password, accept_invite_oauth.
- [x] Handler — 2026-06-25
      POST /auth/register (auth + attendant/doctor/admin), POST /auth/register/doctor (público), POST /auth/login, POST /auth/refresh, POST /auth/logout, POST /auth/invite/accept/password, POST /auth/invite/accept/oauth. GET /auth/oauth/google + /callback com goth. Swagger em cada handler.
- [x] Cache — 2026-06-25
      JWT middleware em internal/shared/middleware/jwt.go — lê Bearer token, injeta Claims no context via auth.SetClaims. Aplicado globalmente no main.go.
- [x] Arquivo .http — 2026-06-25: references/http/03-auth.http

## Critério de conclusão

- Domínio testado
- Usuários com login conseguem autenticar via password e OAuth
- Middleware JWT disponível para os demais módulos
