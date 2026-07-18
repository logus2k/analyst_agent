# Task : Specialty

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`

## Sequência de execução

1. Domínio: specialty, lifecycle, regras de desativação, erros — com testes
2. Migration: tabela de specialties
3. Repositório: persistência, lookup ativo/desativado
4. Use cases: cadastro, edição, desativação, listagem com paginação cursor-based cursor-based e filtro
5. Handler: endpoints de specialty
   - `POST /specialties` — `admin`
   - `PUT /specialties/:id` — `admin`
   - `PATCH /specialties/:id/deactivate` — `admin`
   - `GET /specialties` — `admin`, `doctor`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: listagem de specialties ativas)

## Restrições

- Apenas admin gerencia specialties
- Sem hard delete
- Porta de lookup disponível para outros módulos

## Progresso

- [x] Domínio — 2026-06-25
      Entidade `Specialty` (ID, PublicID, Name, Active). `New` inicia como active. `Update(name)`, `Deactivate()` com invariante. 6 testes cobrindo criação, edição e desativação.
- [x] Migration — 2026-06-25
      `000004_create_specialties.up.sql`: tabela `specialties` com índice parcial `WHERE active = TRUE`.
- [x] Repositório — 2026-06-25
      infra/repository.go com cache Redis por publicID (TTL 5min), invalidado no Update. `IsActive()` cacheado para lookup de outros módulos.
- [x] Use cases — 2026-06-25
      CreateSpecialty (verifica unicidade de nome), UpdateSpecialty (verifica unicidade apenas se nome mudou), DeactivateSpecialty, ListSpecialties, GetSpecialty.
- [x] Handler — 2026-06-25
      POST /specialties (admin), PUT /specialties/{id} (admin), PATCH /specialties/{id}/deactivate (admin), GET /specialties (admin, doctor). Swagger em cada handler.
- [x] Cache — 2026-06-25
      `IsActive()` cacheado em Redis. Porta `SpecialtyLookup` exposta via `Module.Lookup()` para injeção em outros módulos (doctor-specialty, prescription).
- [x] Arquivo .http — 2026-06-25: references/http/05-specialty.http

## Critério de conclusão

- Domínio testado
- Gestão de specialties pelo admin funcional
- Porta de lookup disponível
