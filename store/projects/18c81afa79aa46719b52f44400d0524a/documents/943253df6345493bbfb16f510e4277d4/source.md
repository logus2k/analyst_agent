# Task : Doctor Specialty

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `05-doctor`, `05-specialty`

## Sequência de execução

1. Domínio: atribuição doctor-specialty, lifecycle de aprovação, elegibilidade, erros — com testes
2. Migration: tabela de atribuições doctor-specialty
3. Repositório: persistência, lookup de elegibilidade aprovada
4. Use cases: médico solicita associação, admin aprova/rejeita, porta de elegibilidade para prescrições
5. Handler: endpoints de doctor-specialty
   - `POST /doctors/:id/specialties` — `doctor`
   - `PATCH /doctors/:id/specialties/:specialtyId/approve` — `admin`
   - `PATCH /doctors/:id/specialties/:specialtyId/reject` — `admin`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: elegibilidade aprovada)

## Restrições

- Médico solicita — admin aprova
- Elegibilidade de doctor em `User` é separada da aprovação de specialty
- Referência ao doctor usa `users`, não tabela separada
- Sem hard delete

## Progresso

- [x] Domínio — 2026-06-25
      Entidade `DoctorSpecialty` com `AssignmentStatus` (pending/approved/rejected). `New()` cria em pending. `Approve()` e `Reject()` exigem status pending. 6 testes cobrindo ciclo completo.
- [x] Migration — 2026-06-25
      `000005_create_doctor_specialties.up.sql`: tabela com UNIQUE (doctor_user_id, specialty_id), índice parcial para aprovados.
- [x] Repositório — 2026-06-25
      `Save()` resolve specialty_id via sub-select na tabela specialties + verifica active=TRUE. `HasApprovedSpecialty()` cacheado em Redis (key `docspec:eligible:{userID}:{specialtyPublicID}`, TTL 5min), invalidado no Update.
- [x] Use cases — 2026-06-25
      `RequestSpecialty` (verifica anti-IDOR + doctor ativo via porta), `ApproveAssignment`, `RejectAssignment`.
- [x] Handler — 2026-06-25
      POST /doctors/{id}/specialties (doctor), PATCH /doctors/{id}/specialties/{specialtyId}/approve (admin), PATCH /doctors/{id}/specialties/{specialtyId}/reject (admin). Swagger em cada handler.
- [x] Cache — 2026-06-25
      `HasApprovedSpecialty()` cacheado. Porta `SpecialtyEligibility` exposta via `Module.Eligibility()` para o módulo de prescrição. `DoctorEligibilityChecker` recebido por injeção do módulo doctor.
- [x] Arquivo .http — 2026-06-25: references/http/06-doctor-specialty.http

## Critério de conclusão

- Domínio testado
- Fluxo de solicitação e aprovação funcional
- Porta de elegibilidade disponível para o módulo de prescrição
