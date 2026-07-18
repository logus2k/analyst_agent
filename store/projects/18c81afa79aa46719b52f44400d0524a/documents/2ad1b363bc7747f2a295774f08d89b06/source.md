# Task : Finalização

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** todas as tasks anteriores

## Sequência de execução

1. Rodar `swag init` para consolidar anotações Swagger já existentes nos handlers
2. Verificar Swagger UI via `swaggo/http-swagger`
3. Rodar todos os testes
4. Verificar build
5. Verificar aplicação de migrations
6. Revisar boundaries de módulos
7. Revisar compliance com regras de negócio do `sdd.md`
8. Revisar segurança e auth
9. Revisar gaps de teste

## Progresso

- [x] Consolidação Swagger — 2026-06-25: `swag init` gerou docs.go, swagger.json, swagger.yaml sem erros; 35+ tipos gerados
- [x] Testes completos — 2026-06-25: 55 testes de domínio passando (12 módulos), `go test ./...` sem falhas
- [x] Verificação de build — 2026-06-25: `go build ./...` passa sem warnings
- [x] Verificação de migrations — 2026-06-25: 11 migrations sequenciais (000001–000011), todas com .up.sql e .down.sql, FKs em ordem correta
- [x] Revisão de boundaries — 2026-06-25: zero imports cross-module; infra lê tabelas de outros módulos diretamente no banco conforme convenção
- [x] Revisão de segurança — 2026-06-25: todas as rotas protegidas por RequireRole exceto auth/* e GET /prescriptions/download/{token} (link público intencional); zero SQL injection (queries parametrizadas); zero string formatting em SQL
- [x] Revisão de gaps de teste — 2026-06-25: testes de domínio cobrem lifecycle de todas as entidades; gap conhecido: testes de integração de infra e use cases (fora do escopo MVP)

## Critério de conclusão

- `go build ./...` passa
- Todos os testes passam
- Migrations aplicam sem erro
- Swagger gerado com sucesso
- Sem findings críticos ou de alta severidade em aberto
