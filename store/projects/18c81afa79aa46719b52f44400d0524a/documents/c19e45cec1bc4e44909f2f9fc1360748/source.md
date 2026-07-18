# Task 01: Setup

**Skill:** `go-conventions`
**Referência:** `references/architecture.md`

## Sequência de execução

1. Estrutura de pastas do projeto
2. Composition root com montagem de módulos
3. Configuração via variáveis de ambiente com validação no startup
4. Conexão com PostgreSQL em `internal/shared/db/`
5. Conexão com Redis em `internal/shared/cache/`
6. Graceful shutdown
7. Primitivos compartilhados: ID interno, ID público, erros, paginação, filtros, contexto de auth
8. Fundação de migrations com `golang-migrate`
9. Rota do Swagger UI (`/swagger/*`) registrada no router
10. Makefile com targets: `run`, `build`, `test`, `migrate-up`, `migrate-down`, `swag`


## Progresso

- [x] Estrutura de pastas — 2026-06-25
      Diretórios cmd/, db/migrations/, docs/, internal/shared/{cache,db,httperr,middleware,validator,id,auth,page}, internal/modules/ criados.
- [x] Composition root — 2026-06-25
      cmd/main.go monta config, db, cache, router Chi e módulos futuros. Rota Swagger registrada.
- [x] Configuração — 2026-06-25
      internal/shared/config/config.go com envconfig e validação no startup (required:"true").
- [x] Conexão PostgreSQL — 2026-06-25
      internal/shared/db/db.go com pgxpool.New + Ping.
- [x] Conexão Redis — 2026-06-25
      internal/shared/cache/cache.go com redis.ParseURL + Ping.
- [x] Graceful shutdown — 2026-06-25
      signal.NotifyContext (SIGINT/SIGTERM) com timeout de 5s em cmd/main.go.
- [x] Primitivos compartilhados — 2026-06-25
      id/ (snowflake + nanoid), auth/ (Claims + context helper), httperr/ (Write + WriteDetails), validator/ (instância compartilhada), page/ (Cursor + Page[T]), middleware/ (RequireRole).
- [x] Fundação de migrations — 2026-06-25
      db/migrations/ criado com .gitkeep. Targets migrate-up/migrate-down no Makefile usam golang-migrate CLI.
- [x] Rota Swagger UI — 2026-06-25
      GET /swagger/* registrado com httpSwagger.Handler(). docs/docs.go com stub compilável.
- [x] Makefile — 2026-06-25
      Targets: run, build, test, migrate-up, migrate-down, swag. Carrega .env automaticamente.

## Critério de conclusão

- `go build ./...` passa sem erros
- App inicializa e conecta ao banco e ao cache
- App encerra limpo ao receber sinal de término
- Migrations podem ser aplicadas localmente
