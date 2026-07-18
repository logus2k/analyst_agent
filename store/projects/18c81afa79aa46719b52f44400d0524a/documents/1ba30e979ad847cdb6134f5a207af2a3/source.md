# Architecture — Lootify

## Module Path

```
github.com/dassishot/lootify
```

## Estilo Arquitetural

Modular monolith com DDD.
Módulos são organizados por capacidade de negócio — não por camada técnica.

## Layout

```
cmd/
db/
  migrations/
internal/
  shared/
    cache/
    db/
    httperr/
    middleware/
    validator/
  modules/
    user/
    auth/
    doctor/
    specialty/
    doctor-specialty/
    medication/
    prescription/
    documents/
    share/
    attendant-doctor/
    schedule/
```

## Estrutura de Módulo

Cada módulo tem exatamente quatro pacotes:

```
<modulo>/
├── application/   # use cases
├── domain/        # entidades, interfaces, erros de domínio
├── handler/       # HTTP handlers
└── infra/         # repositório e acessos externos
```

## Entrypoint de Módulo

Cada módulo expõe uma struct `Module` com um construtor `NewModule` e o método `Routes() chi.Router`.
Dependências entre módulos são passadas via `NewModule`.
O `main` instancia os módulos e monta as rotas.

## Código Compartilhado

`internal/shared/` contém apenas código genérico e reutilizável.
Regras de negócio pertencem ao módulo dono.

## Bibliotecas

| Responsabilidade | Biblioteca |
|---|---|
| Router | `chi` |
| Banco de dados | `pgx/v5` |
| Cache | Redis |
| Migrations | `golang-migrate` |
| Validação | `go-playground/validator v10` |
| JWT | `golang-jwt/jwt` |
| OAuth | `goth` |
| PDF | a definir |
| Assinatura digital | a definir (PAdES, ICP-Brasil) |
| API docs | `swaggo/swag` + `swaggo/http-swagger` |

## Padrões

- Injeção de dependência manual
- Um use case por arquivo — struct + Command + Execute no mesmo arquivo
- Um handler por arquivo
- Entidades com comportamento — regras de negócio na entidade, não no service
- Validação de input no handler, antes do use case
- Erros HTTP tipados com writer central em `httperr`
- Middleware JWT global aplicado no `main`
- Claims do usuário autenticado injetadas no `context.Context`

## Persistência

- PostgreSQL como fonte de verdade
- Redis para cache — nunca requisito de corretude
- Sem hard delete — lifecycle via `deleted_at`
- IDs internos (snowflake) e IDs públicos (nanoid) separados
- IDs públicos nunca expõem IDs internos

## Estrutura de Tabelas

- `users` — base de todos os usuários. Todo user é paciente.
- `doctors` — 1x1 com `users`. `user_id` é PK e FK ao mesmo tempo.
- `auth` — exclusiva do módulo `auth`. Contém `user_id` como referência a `users`.
- `specialties` — exclusiva do módulo `specialty`.
- `doctor_specialties` — exclusiva do módulo `doctor-specialty`. Referencia `users.id` como `doctor_id`.
- Módulos com dados próprios definem sua própria tabela.
- Em relacionamentos 1x1, use a PK da tabela pai como PK e FK da tabela filha.
- Módulos podem ler tabelas de outros módulos no `infra/` — apenas os campos necessários.

## Migrations

- Arquivos em `db/migrations/`
- Uma migration por alteração de schema
- `.up.sql` e `.down.sql` por migration
- Executadas via CLI do `golang-migrate`, invocado pelos targets do Makefile (`migrate-up`, `migrate-down`)
- Rollback não pode causar perda de dados em produção

## Segurança

- Chaves ICP-Brasil (A1/A3) armazenadas no sistema
- Assinatura digital no padrão PAdES
- Public IDs expostos nas APIs — nunca IDs internos
- Autorização explícita em cada use case

## Testes de API

Use REST Client (plugin VS Code) para testar endpoints.
Arquivos `.http` criados em `references/http/` — um arquivo por módulo.
Criar os arquivos `.http` após implementar cada handler.

## Variáveis de Ambiente

Credenciais e configurações sensíveis são carregadas via variáveis de ambiente — nunca hardcoded.
O Makefile carrega o arquivo `.env` antes de executar qualquer target.

Variáveis obrigatórias para Google OAuth:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_CALLBACK_URL`
