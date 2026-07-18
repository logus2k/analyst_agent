# Task : Medication

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`

## Sequência de execução

1. Domínio: medication, lifecycle, regras de desativação, erros — com testes
2. Migration: tabela de medications
3. Repositório: persistência, lookup ativo/desativado
4. Use cases: cadastro, edição, desativação, listagem com paginação cursor-based cursor-based e filtro
5. Handler: endpoints de medication
   - `POST /medications` — `admin`
   - `PUT /medications/:id` — `admin`
   - `PATCH /medications/:id/deactivate` — `admin`
   - `GET /medications` — `admin`, `doctor`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: listagem de medications ativas)

## Restrições

- Apenas admin gerencia medications
- `attendant` não tem acesso a medications
- Sem hard delete
- Atualização de medication não muda prescrições já emitidas
- Porta de lookup disponível para outros módulos

## Progresso

- [x] Domínio — 2026-06-25
      Entidade `Medication` (ID, PublicID, Name, Active). `New` inicia como active. `Update(name)`, `Deactivate()` com invariante. 6 testes passando.
- [x] Migration — 2026-06-25
      `000006_create_medications.up.sql`: tabela com índice parcial `WHERE active = TRUE`.
- [x] Repositório — 2026-06-25
      infra/repository.go com cache Redis por publicID (TTL 5min), invalidado no Update. `IsActive()` cacheado para lookup port.
- [x] Use cases — 2026-06-25
      CreateMedication (verifica unicidade de nome), UpdateMedication (verifica unicidade apenas se nome mudou), DeactivateMedication, ListMedications.
- [x] Handler — 2026-06-25
      POST /medications (admin), PUT /medications/{id} (admin), PATCH /medications/{id}/deactivate (admin), GET /medications (admin, doctor). Swagger em cada handler.
- [x] Cache — 2026-06-25
      `IsActive()` cacheado em Redis. Porta `MedicationLookup` exposta via `Module.Lookup()` para uso pelo módulo de prescrição.
- [x] Arquivo .http — 2026-06-25: references/http/07-medication.http

## Critério de conclusão

- Domínio testado
- Gestão de medications pelo admin funcional
- Porta de lookup disponível
