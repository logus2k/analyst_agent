# Task 02: User

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `01-setup`

## Sequência de execução

1. Domínio: `User`, roles, lifecycle, regras de identidade — com testes
2. Migration: schema de users
3. Repositório: persistência e lookup de users
4. Use cases: registro de user, atualização de dados do user, atribuição de role, lookup, listagem com paginação cursor-based e filtro
5. Handler: endpoints de user
   - `POST /users` — `attendant`, `doctor`
   - `GET /users` — `admin`, `attendant`, `doctor`
   - `GET /users/:id` — `admin`, `attendant`, `doctor`
   - `PUT /users/:id` — `admin`, `attendant`, `doctor`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Seed do primeiro `admin`
7. Cache: onde aplicável (ex: lookup de user por ID público)

## Restrições

- Todo `User` é automaticamente um patient
- Roles permitidas: `admin`, `attendant`, `doctor`
- Patient não tem login por padrão
- `User.register` deve ser único
- `attendant` pode listar e buscar users
- Sem hard delete

## Progresso

- [x] Domínio — 2026-06-25
      Entidade `User` com comportamento (New, AssignRole, Update), `Role` como value object, erros de domínio. 13 testes cobrindo invariantes de identidade, roles e atualização.
- [x] Migration — 2026-06-25
      `000001_create_users.up.sql` com tabela users (id BIGINT snowflake, public_id nanoid, name, register UNIQUE, role nullable, timestamps, deleted_at).
- [x] Repositório — 2026-06-25
      `infra/repository.go` com pgxpool: Save, FindByID, FindByPublicID (com cache Redis TTL 5min), FindByRegister, Update (invalida cache), List cursor-based.
- [x] Use cases — 2026-06-25
      RegisterUser, UpdateUser, AssignRole, GetUser, ListUsers — um arquivo por use case.
- [x] Handler — 2026-06-25
      POST /users (attendant, doctor), GET /users, GET /users/{id}, PUT /users/{id} (admin/attendant/doctor). Anotações Swagger em cada handler. Role assignment via PUT apenas para admin.
- [x] Seed admin — 2026-06-25
      `cmd/seed/main.go` insere primeiro admin (register: admin-001) com ON CONFLICT DO NOTHING. Target `make seed` no Makefile.
- [x] Cache — 2026-06-25
      FindByPublicID cacheado no Redis (key `user:{publicID}`, TTL 5min). Update invalida a chave.
- [x] Arquivo .http — 2026-06-25: references/http/02-user.http

## Critério de conclusão

- Domínio testado
- Attendant consegue registrar users
- Admin e doctor conseguem listar e buscar users
